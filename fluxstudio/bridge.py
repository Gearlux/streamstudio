"""Dynamic runtime generation of ComfyUI nodes from DataFlux/HF operations."""

import inspect
from typing import Any, Callable, Dict, Optional, Tuple, Type

import datasets
from dataflux.discovery import scan_module
from logflow import get_logger

logger = get_logger(__name__)

# Mapping Python types to ComfyUI widget types
TYPE_MAP = {
    int: "INT",
    float: "FLOAT",
    str: "STRING",
    bool: "BOOLEAN",
    datasets.Dataset: "HF_DATASET",
}

DEFAULT_WIDGET_CONFIG: Dict[str, Dict[str, Any]] = {
    "INT": {"default": 0},
    "FLOAT": {"default": 0.0, "step": 0.01},
    "STRING": {"default": "", "multiline": False},
    "BOOLEAN": {"default": False},
}


def _map_type(param_name: str, annotation: Any) -> str:
    """Map Python type annotation to ComfyUI type string."""
    if annotation == datasets.Dataset:
        return "HF_DATASET"

    str_ann = str(annotation)

    # Heuristic for directory/file paths
    if "pathlib.Path" in str_ann or any(x in param_name.lower() for x in ("path", "dir", "root", "folder")):
        return "PATH"

    if annotation in TYPE_MAP:
        return TYPE_MAP[annotation]
    # Handle optional types and unions (simplistic)
    if "datasets.arrow_dataset.Dataset" in str_ann or "datasets.Dataset" in str_ann:
        return "HF_DATASET"
    return "STRING"  # Fallback


def create_comfy_node(
    func: Callable,
    name: str,
    category: str = "Taidal",
    display_name: Optional[str] = None,
) -> Type:
    """Dynamically construct a ComfyUI node class from a Python callable."""
    sig = inspect.signature(func)
    inputs: Dict[str, Dict[str, Any]] = {"required": {}}
    return_types = []

    for param_name, param in sig.parameters.items():
        comfy_type = _map_type(param_name, param.annotation)

        if comfy_type == "HF_DATASET":
            inputs["required"][param_name] = (comfy_type,)
        elif comfy_type == "PATH":
            # ComfyUI's STRING type can be hinted to be a directory selector
            # by using an undocumented feature or extensions, but the most common
            # approach for absolute paths without extensions is a multiline string
            # or a specific custom JS widget. We will use a standard string for now,
            # but provide a hook for a future JS extension by setting a custom class.
            config = {
                "default": param.default if param.default is not inspect.Parameter.empty else "",
                "taidal_widget": "DIRECTORY",
            }
            inputs["required"][param_name] = ("STRING", config)
            # In the future, we can inject a custom widget here if the frontend supports it.
        else:
            config = DEFAULT_WIDGET_CONFIG.get(comfy_type, {}).copy()
            if param.default is not inspect.Parameter.empty:
                config["default"] = param.default
            inputs["required"][param_name] = (comfy_type, config)

    # Determine return type (simplistic: assume first output is HF_DATASET if annotated)
    ret_ann = sig.return_annotation
    if _map_type("return", ret_ann) == "HF_DATASET":
        return_types.append("HF_DATASET")
    else:
        return_types.append("STRING")

    def execute_wrapper(self: Any, **kwargs: Any) -> Tuple[Any, ...]:
        result = func(**kwargs)
        if isinstance(result, tuple):
            return result
        return (result,)

    node_class = type(
        name,
        (object,),
        {
            "INPUT_TYPES": classmethod(lambda cls: inputs),
            "RETURN_TYPES": tuple(return_types),
            "FUNCTION": "execute",
            "CATEGORY": category,
            "execute": execute_wrapper,
        },
    )
    return node_class


def register_module_nodes(
    module_path: str,
    target_mappings: Dict[str, Type],
    display_name_mappings: Dict[str, str],
    category: str = "Taidal",
) -> None:
    """Scan a module and register all compatible callables as ComfyUI nodes."""
    logger.info(f"ComfyFlux: Scanning module {module_path} for nodes...")
    discovered = scan_module(module_path)

    from dataflux.discovery import resolve_callable

    for item in discovered:
        target_func = resolve_callable(item["path"])
        if not callable(target_func):
            continue

        node_name = f"Taidal_{item['name']}"
        node_class = create_comfy_node(
            func=target_func,
            name=node_name,
            category=category,
        )
        target_mappings[node_name] = node_class
        display_name_mappings[node_name] = item["name"].replace("_", " ").title()
        logger.debug(f"ComfyFlux: Registered node {node_name} for {item['path']}")
