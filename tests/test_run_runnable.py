"""``run_runnable`` honours the duck-typed ``__needs_autograd__`` flag.

ComfyUI evaluates the whole graph under ``torch.inference_mode()``, which is fatal for
gradient-based training (params / activations / the loss become inference tensors, so
``loss.backward()`` raises *"element 0 of tensors does not require grad"*). A runnable that
declares ``__needs_autograd__`` gets normal autograd restored for the duration of ``run()``;
everything else runs as-is under inference mode.

The flag is read by NAME (``getattr(runnable, "__needs_autograd__", False)``), so the read
fails OPEN: a rename on either side of this cross-package contract silently disables training
rather than raising. That is what these tests pin.
"""

import torch
from recordstream.runnable import TorchRunner

from streamstudio.nodes import run_runnable


class _Trainer(TorchRunner):
    """Declares the flag via the mixin — the executor must restore autograd around run()."""

    def __init__(self) -> None:
        self.grad_enabled: bool = False
        self.inference: bool = True

    def run(self) -> None:
        self.grad_enabled = torch.is_grad_enabled()
        self.inference = torch.is_inference_mode_enabled()


class _Evaluator:
    """No flag at all — an inference-only runnable stays under ComfyUI's inference mode."""

    def __init__(self) -> None:
        self.inference: bool = False

    def run(self) -> None:
        self.inference = torch.is_inference_mode_enabled()


class _PerTask(TorchRunner):
    """The merged train+eval shape: the flag is a per-task property, not a constant."""

    def __init__(self, task: str) -> None:
        self.task = task
        self.grad_enabled: bool = False

    @property
    def __needs_autograd__(self) -> bool:  # type: ignore[override]
        return self.task == "fit"

    def run(self) -> None:
        self.grad_enabled = torch.is_grad_enabled()


def test_needs_autograd_runnable_gets_autograd_restored() -> None:
    trainer = _Trainer()
    with torch.inference_mode():
        run_runnable(trainer, None)
    assert trainer.grad_enabled is True
    assert trainer.inference is False


def test_runnable_without_the_flag_stays_under_inference_mode() -> None:
    evaluator = _Evaluator()
    with torch.inference_mode():
        run_runnable(evaluator, None)
    assert evaluator.inference is True


def test_per_task_property_decides_per_instance() -> None:
    fitting, testing = _PerTask("fit"), _PerTask("test")
    with torch.inference_mode():
        run_runnable(fitting, None)
        run_runnable(testing, None)
    assert fitting.grad_enabled is True
    assert testing.grad_enabled is False
