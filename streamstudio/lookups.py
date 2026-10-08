"""The lists an installed PLUGIN supplies beside a field — a lookup picker the editor does not know by name.

A producer stamps one of its parameters with a tag of its own (``__taidal_widget_params__ = {"dataset":
"SHOP_DATASET"}``, the stamp :func:`streamstudio.palette.picker_of` already reads). Until now the canvas page drew a
tag it did not know as a plain field. A ``streamstudio.plugins`` module — the plugin seam the ComfyUI editor reads for
``ROUTES`` and ``WEB_DIRECTORY`` — may now also declare::

    LOOKUPS = {"SHOP_DATASET": list_shop_datasets}

where each function takes the node's current values (``{param: text}``, so a list may depend on the field beside it:
the versions of the dataset named there) and answers ``(names, reason)`` — the reason saying, in words, why the list
is empty when it is. The palette marks such a field ``lookup: true`` (:func:`lists`), and the page asks
``GET api/lookups/<tag>?<the node's values>`` (:func:`answer`) when the person reaches for the list.

A list is a convenience over the free-text field, never a requirement: it never fails a request. A tag no plugin
lists, a function that raises, and one that does not answer within :data:`TIMEOUT_S` (a remote service that hangs on
a login) all answer an empty list with the reason.
"""

import importlib
import importlib.metadata
import threading
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

from loggair import get_logger

logger = get_logger(__name__)

#: What a plugin's list function answers: the names, and why there are none (``""`` when there are).
Listed = Tuple[Sequence[str], str]
#: A plugin's list function — the node's current values in, ``(names, reason)`` (or the bare names) out.
LookupFn = Callable[[Mapping[str, str]], Union[Listed, Sequence[str]]]

#: The entry-point group a plugin declares itself in — the one the ComfyUI editor reads for its routes.
PLUGIN_GROUP = "streamstudio.plugins"
#: The module attribute a plugin's lists are read from.
LOOKUPS_ATTRIBUTE = "LOOKUPS"
#: How long a list may take before the field is left without it — a remote list can block on a login prompt.
TIMEOUT_S = 20.0

#: The plugins' lists, by tag — read once per process (:func:`plugin_lookups`); ``None`` until then.
_FOUND: Optional[Dict[str, LookupFn]] = None
_FOUND_LOCK = threading.Lock()


def _entry_points() -> Iterable[Any]:  # importlib.metadata.EntryPoint
    return importlib.metadata.entry_points(group=PLUGIN_GROUP)


def plugin_lookups() -> Dict[str, LookupFn]:
    """Every plugin's lists, by tag — the ``LOOKUPS`` of each ``streamstudio.plugins`` module, read once per process.

    A plugin is an explicit opt-in, so one that fails to import is a ``warning`` naming the module (and is skipped); a
    module with no ``LOOKUPS`` is simply a plugin that lists nothing, and an entry that is not a function is skipped.
    """
    global _FOUND
    with _FOUND_LOCK:
        if _FOUND is not None:
            return _FOUND
        found: Dict[str, LookupFn] = {}
        for entry in _entry_points():
            try:
                module = importlib.import_module(entry.value)
            except Exception as exc:  # noqa: BLE001 - one broken plugin never costs the others their lists
                logger.warning(
                    f"StreamStudio plugin {entry.name} ({entry.value}) failed to load: {type(exc).__name__}: {exc}"
                )
                continue
            table = getattr(module, LOOKUPS_ATTRIBUTE, None)
            if not isinstance(table, Mapping):
                continue
            for tag, function in table.items():
                if callable(function):
                    found[str(tag)] = function
                else:
                    logger.debug(f"StreamStudio plugin {entry.name}: {LOOKUPS_ATTRIBUTE}[{tag!r}] is no function")
        _FOUND = found
        return found


def lists(tag: str) -> bool:
    """Whether an installed plugin supplies a list for the picker ``tag``."""
    return tag in plugin_lookups()


def _normalised(answered: Any) -> Tuple[List[str], str]:  # a plugin's answer, whatever it returned
    names: Any = answered
    reason = ""
    if isinstance(answered, tuple) and len(answered) == 2 and isinstance(answered[1], str):
        names, reason = answered
    if isinstance(names, (str, bytes)) or not isinstance(names, Iterable):
        return [], reason or f"the list answered {type(names).__name__}, not names"
    return [str(name) for name in names if name is not None], reason


def answer(tag: str, values: Mapping[str, str]) -> Dict[str, Any]:
    """``{names, reason}`` — the list the plugin supplies for ``tag``, asked with the node's ``values``.

    Never raises: no plugin listing the tag, a list that raises and one that takes longer than :data:`TIMEOUT_S`
    each answer no names and say why. The list runs on a daemon thread so a hang leaves the request, not the server.
    """
    function = plugin_lookups().get(tag)
    if function is None:
        return {"names": [], "reason": f"no installed plugin lists {tag}"}
    result: Dict[str, Any] = {}

    def ask() -> None:
        try:
            result["answered"] = function(dict(values))
        except Exception as exc:  # noqa: BLE001 - a list never fails the request; the reason says what happened
            result["error"] = f"{type(exc).__name__}: {exc}"

    worker = threading.Thread(target=ask, name=f"lookup-{tag}", daemon=True)
    worker.start()
    worker.join(TIMEOUT_S)
    if worker.is_alive():
        logger.debug(f"lookup {tag}: no answer within {TIMEOUT_S:g} s")
        return {"names": [], "reason": f"the list did not answer within {TIMEOUT_S:g} s"}
    if "error" in result:
        logger.debug(f"lookup {tag}: {result['error']}")
        return {"names": [], "reason": result["error"]}
    names, reason = _normalised(result.get("answered"))
    return {"names": names, "reason": reason}


__all__ = ["LOOKUPS_ATTRIBUTE", "LookupFn", "PLUGIN_GROUP", "TIMEOUT_S", "answer", "lists", "plugin_lookups"]
