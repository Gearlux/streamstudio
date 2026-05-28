import datasets
import pytest
from fluxstudio.bridge import create_comfy_node

def test_create_comfy_node():
    def my_op(dataset: datasets.Dataset, threshold: float = 0.5) -> datasets.Dataset:
        """My sample op."""
        return dataset.filter(lambda x: x["val"] > threshold)

    NodeClass = create_comfy_node(my_op, "MyOpNode")
    
    # Check ComfyUI contract
    assert hasattr(NodeClass, "INPUT_TYPES")
    inputs = NodeClass.INPUT_TYPES()
    assert "dataset" in inputs["required"]
    assert inputs["required"]["dataset"] == ("HF_DATASET",)
    assert "threshold" in inputs["required"]
    assert inputs["required"]["threshold"][0] == "FLOAT"
    assert inputs["required"]["threshold"][1]["default"] == 0.5
    
    assert NodeClass.RETURN_TYPES == ("HF_DATASET",)
    assert NodeClass.FUNCTION == "execute"
    assert NodeClass.CATEGORY == "Taidal"

def test_node_execution():
    def add_val(dataset: datasets.Dataset, amount: int = 1) -> datasets.Dataset:
        return dataset.map(lambda x: {"val": x["val"] + amount})
    
    NodeClass = create_comfy_node(add_val, "AddValNode")
    node = NodeClass()
    
    ds = datasets.Dataset.from_dict({"val": [1, 2, 3]})
    result = node.execute(dataset=ds, amount=10)
    
    assert isinstance(result, tuple)
    assert len(result) == 1
    assert result[0]["val"] == [11, 12, 13]
