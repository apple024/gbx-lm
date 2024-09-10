import unittest

import mlx.nn as nn
from gbx_lm.models.qqwen2 import Model as Qwen2Model
from gbx_lm.utils import get_model_path, load_model

HF_MODEL_PATH = "GreenBitAI/Qwen-1.5-0.5B-Chat-layer-mix-bpw-2.2-mlx"


class TestLoadModelCustomGetClasses(unittest.TestCase):

    def test_load_model_with_custom_get_classes(self):
        class CustomQwenModel(nn.Module):
            def __init__(self, args):
                super().__init__()
                self.config = args
                self.custom_attribute = "This is a custom model"

            def load_weights(self, weights, strict=True):
                self.qwenWeights = weights

        class CustomQwenConfig:
            @classmethod
            def from_dict(cls, config):
                instance = cls()
                for k, v in config.items():
                    setattr(instance, k, v)
                return instance

        def custom_get_classes(config):
            return CustomQwenModel, CustomQwenConfig

        model_path = get_model_path(HF_MODEL_PATH)
        model = load_model(model_path, get_model_classes=custom_get_classes)

        self.assertIsInstance(model, CustomQwenModel)
        self.assertTrue(hasattr(model, "custom_attribute"))
        self.assertEqual(model.custom_attribute, "This is a custom model")
        self.assertTrue(hasattr(model, "qwenWeights"))

    def test_load_model_with_default_get_classes(self):
        model_path = get_model_path(HF_MODEL_PATH)
        model = load_model(model_path)

        self.assertIsInstance(model, Qwen2Model)


if __name__ == "__main__":
    unittest.main()
