"""Static ComfyUI adapter nodes for FluxStudio: walk a DataFlux source by index, and view a Sample.

These are ComfyUI orchestration/adapter glue — NOT DataFlux data ops — so they live here rather
than in dataflux / waivefront. The actual rendering is delegated to
``waivefront.visualizers.sample_to_image`` (per the "no FluxStudio-only visualizers" mandate); this
module only bridges a DataFlux ``Sample`` to ComfyUI's image-preview protocol and provides the
index-walk stepper.
"""

import os
from typing import Any, Dict, List, Tuple, Type

import numpy as np
import torch
from logflow import get_logger

from fluxstudio.bridge import SAMPLE_TYPE, SOURCE_TYPE

logger = get_logger(__name__)

# Matches ComfyUI's seed-widget convention so the frontend renders a control_after_generate spinner.
_MAX_INDEX = 0xFFFFFFFFFFFFFFFF


class WalkDatasetNode:
    """Emit the ``Sample`` at ``index`` from a ``DATAFLUX_SOURCE``; ``index`` auto-increments each run.

    The ``index`` widget uses ComfyUI's ``control_after_generate`` so every Queue Prompt advances to
    the next sample (set the control to "fixed" / index 0 to reset). With ``wrap`` enabled the index
    wraps modulo the source length; otherwise an out-of-range index raises.
    """

    CATEGORY = "Taidal/FluxStudio"
    RETURN_TYPES = (SAMPLE_TYPE,)
    RETURN_NAMES = ("sample",)
    FUNCTION = "walk"

    @classmethod
    def INPUT_TYPES(cls) -> Dict[str, Any]:
        return {
            "required": {
                "source": (SOURCE_TYPE,),
                "index": ("INT", {"default": 0, "min": 0, "max": _MAX_INDEX, "control_after_generate": True}),
                "wrap": ("BOOLEAN", {"default": True}),
            }
        }

    def walk(self, source: Any, index: int, wrap: bool = True) -> Tuple[Any, ...]:
        from dataflux.core import Flux

        flux = source if isinstance(source, Flux) else Flux.from_source(source)
        length = len(flux)
        if length <= 0:
            raise RuntimeError("WalkDatasetNode: source is empty (len == 0); nothing to walk.")
        if wrap:
            resolved = index % length
        elif not 0 <= index < length:
            raise IndexError(
                f"WalkDatasetNode: index {index} out of range for source of length {length} "
                "(enable 'wrap' to cycle)."
            )
        else:
            resolved = index
        logger.debug(f"WalkDatasetNode: yielding sample {resolved}/{length}")
        return (flux[resolved],)


class SampleViewerNode:
    """Render a ``DATAFLUX_SAMPLE`` in-canvas and pass through an IMAGE + info STRING.

    ``OUTPUT_NODE``: previews ``sample.input`` as an image (via
    ``waivefront.visualizers.sample_to_image``) and surfaces ``sample.target`` /
    ``sample.metadata`` as text. Also returns the rendered IMAGE (chainable to native ComfyUI image
    nodes) and an ``info`` STRING.
    """

    CATEGORY = "Taidal/FluxStudio"
    RETURN_TYPES = ("IMAGE", "STRING")
    RETURN_NAMES = ("image", "info")
    FUNCTION = "view"
    OUTPUT_NODE = True

    @classmethod
    def INPUT_TYPES(cls) -> Dict[str, Any]:
        return {
            "required": {
                "sample": (SAMPLE_TYPE,),
                "colormap": ("STRING", {"default": "viridis"}),
                "max_size": ("INT", {"default": 512, "min": 32, "max": 4096}),
            }
        }

    def view(self, sample: Any, colormap: str = "viridis", max_size: int = 512) -> Dict[str, Any]:
        rgb = self._render(sample, colormap, max_size)
        info = self._describe(sample)
        image_tensor = torch.from_numpy(rgb.astype(np.float32) / 255.0)[None,]
        ui_images = self._save_preview(rgb)
        return {"ui": {"images": ui_images, "text": [info]}, "result": (image_tensor, info)}

    @staticmethod
    def _render(sample: Any, colormap: str, max_size: int) -> np.ndarray:
        try:
            from waivefront.visualizers import sample_to_image
        except ImportError as exc:  # pragma: no cover - exercised only without the extra installed
            raise ImportError(
                "SampleViewerNode needs waivefront to render samples. "
                "Install the extra: pip install 'fluxstudio[waivefront]'."
            ) from exc
        return sample_to_image(sample, colormap=colormap, max_size=max_size)

    @staticmethod
    def _describe(sample: Any) -> str:
        target = getattr(sample, "target", None)
        metadata = getattr(sample, "metadata", {}) or {}
        meta_str = ", ".join(f"{k}={v}" for k, v in list(metadata.items())[:8])
        return f"target={target!r}\nmetadata: {meta_str}"

    @staticmethod
    def _save_preview(rgb: np.ndarray) -> List[Dict[str, Any]]:
        try:
            import folder_paths  # type: ignore[import-not-found]  # ComfyUI-only module
        except ImportError:
            # Not running inside ComfyUI (e.g. a headless test) — skip the on-disk preview.
            return []
        from PIL import Image

        full_output_folder, filename, counter, subfolder, _ = folder_paths.get_save_image_path(
            "fluxstudio_sample_temp_", folder_paths.get_temp_directory(), rgb.shape[1], rgb.shape[0]
        )
        os.makedirs(full_output_folder, exist_ok=True)
        file = f"{filename}_{counter:05}_.png"
        Image.fromarray(rgb).save(os.path.join(full_output_folder, file), compress_level=1)
        return [{"filename": file, "subfolder": subfolder, "type": "temp"}]


# Statically-defined adapter nodes, merged into the auto-discovered set by fluxstudio/__init__.py.
NODE_CLASS_MAPPINGS: Dict[str, Type] = {
    "Taidal_WalkDataset": WalkDatasetNode,
    "Taidal_ShowSample": SampleViewerNode,
}

NODE_DISPLAY_NAME_MAPPINGS: Dict[str, str] = {
    "Taidal_WalkDataset": "Walk Dataset (by index)",
    "Taidal_ShowSample": "Show Sample",
}
