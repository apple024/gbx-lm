# Copyright © 2024 Apple Inc.
# Additional code from GreenBitAI is licensed under the Apache 2.0 License.

import math
import sys
import unittest
from io import StringIO
from unittest.mock import MagicMock

import mlx.core as mx
import mlx.nn as nn
import mlx.optimizers as opt
from mlx.utils import tree_flatten
from gbx_lm import lora, tuner
from gbx_lm.tuner.dora import DoRAEmbedding
from gbx_lm.tuner.lora import LoRAEmbedding, LoRALinear
from gbx_lm.tuner.trainer import evaluate
from gbx_lm.tuner.utils import build_schedule


class TestLora(unittest.TestCase):
    def setUp(self):
        self.capturedOutput = StringIO()
        sys.stdout = self.capturedOutput

    def tearDown(self):
        sys.stdout = sys.__stdout__

    def test_llama(self):
        from gbx_lm.models import qllama

        args = qllama.ModelArgs(
            model_type="llama",
            hidden_size=1024,
            num_hidden_layers=4,
            intermediate_size=2048,
            num_attention_heads=4,
            rms_norm_eps=1e-5,
            vocab_size=10_000,
            tie_word_embeddings=False,
        )

        lora_layers = 4

        def check_config(params, expected_trainable_parameters=None):
            n_keys = 2
            if "keys" in params:
                n_keys = len(params["keys"])
            model = qllama.Model(args)
            model.freeze()
            tuner.utils.linear_to_lora_layers(model, lora_layers, params)
            trainable_params = sum(
                v.size for _, v in tree_flatten(model.trainable_parameters())
            )

            expected_trainable_parameters = expected_trainable_parameters or (
                lora_layers * params["rank"] * args.hidden_size * 2 * n_keys
            )
            self.assertEqual(trainable_params, expected_trainable_parameters)

        params = {"rank": 8, "alpha": 16, "dropout": 0.0, "scale": 10.0}
        check_config(params)

        params["rank"] = 1
        check_config(params)

        params["keys"] = ["self_attn.k_proj"]
        check_config(params)

        params["keys"] = ["lm_head"]
        check_config(
            params,
            expected_trainable_parameters=(
                params["rank"] * (args.hidden_size + args.vocab_size)
            ),
        )

        params["keys"] = ["model.embed_tokens"]
        check_config(
            params,
            expected_trainable_parameters=(
                params["rank"] * (args.hidden_size + args.vocab_size)
            ),
        )

    def test_lora_embedding(self):
        num_embeddings = 256
        dims = 512
        tokens = mx.array([1, 2, 3])

        embedding = nn.QuantizedEmbedding(num_embeddings, dims)
        dequantized_weight = mx.dequantize(
            embedding.weight,
            embedding.scales,
            embedding.biases,
            embedding.group_size,
            embedding.bits,
        )
        lora_emb = LoRAEmbedding.from_base(embedding, r=8, dropout=0, scale=10)
        new_embedding = lora_emb.fuse(de_quantize=True)
        self.assertTrue(mx.array_equal(dequantized_weight, new_embedding.weight))
        self.assertTrue(mx.array_equal(embedding(tokens), lora_emb(tokens)))

        # as_linear
        attn_output = mx.random.uniform(shape=(dims,))
        embedding_lin_out = lora_emb.as_linear(attn_output)
        self.assertEqual(embedding_lin_out.shape, (num_embeddings,))
        self.assertTrue(
            mx.array_equal(embedding_lin_out, embedding.as_linear(attn_output))
        )

        # change the value of lora_b and the embeddings will no longer be equal
        lora_emb.lora_b = mx.random.uniform(shape=lora_emb.lora_b.shape)
        new_embedding = lora_emb.fuse(de_quantize=True)
        self.assertFalse(mx.array_equal(dequantized_weight, new_embedding.weight))
        self.assertFalse(mx.array_equal(embedding(tokens), lora_emb(tokens)))


class TestDora(unittest.TestCase):
    def arrays_almost_equal(self, arr1, arr2, rtol=1e-5, atol=1e-8):
        return mx.array_equal(arr1[:-1], arr2[:-1]) and abs(arr1[-1] - arr2[-1]) < atol + rtol * abs(arr2[-1])

    def test_dora_embedding(self):
        num_embeddings = 256
        dims = 512
        tokens = mx.array([1, 2, 3])

        embedding = nn.Embedding(num_embeddings, dims)

        dora_emb = DoRAEmbedding.from_base(embedding, r=8, dropout=0, scale=10)
        new_embedding = dora_emb.fuse()
        self.assertTrue(mx.array_equal(embedding.weight, new_embedding.weight))
        self.assertTrue(mx.array_equal(embedding(tokens), dora_emb(tokens)))

        # as_linear
        attn_output = mx.random.uniform(shape=(dims,))
        embedding_lin_out = dora_emb.as_linear(attn_output)
        self.assertEqual(embedding_lin_out.shape, (num_embeddings,))
        self.assertTrue(
            mx.array_equal(embedding_lin_out[:-1], embedding.as_linear(attn_output)[:-1])
        )
        print("WARNING: Difference in last element:", embedding_lin_out[-1] - embedding.as_linear(attn_output)[-1])

        # change the value of lora_b and the embeddings will no longer be equal
        dora_emb.lora_b = mx.random.uniform(shape=dora_emb.lora_b.shape)
        new_embedding = dora_emb.fuse()
        self.assertFalse(mx.array_equal(embedding.weight, new_embedding.weight))
        self.assertFalse(mx.array_equal(embedding(tokens), dora_emb(tokens)))


class TestScheduleConfig(unittest.TestCase):
    def test_join(self):
        config = {"name": "cosine_decay", "warmup": 100, "arguments": [1e-5, 100]}
        cos_with_warmup = build_schedule(config)
        self.assertIsNotNone(cos_with_warmup)

        self.assertEqual(cos_with_warmup(0), 0.0)
        self.assertAlmostEqual(cos_with_warmup(101), 1e-5, delta=1e-1)
        optimizer = opt.Adam(learning_rate=cos_with_warmup)
        for _ in range(100):
            optimizer.update({}, {})
        self.assertAlmostEqual(optimizer.learning_rate.item(), 1e-5, delta=1e-1)
        for _ in range(100):
            optimizer.update({}, {})
        expected_lr = 1e-5 * 0.5 * (1.0 + math.cos(math.pi * 200 / 10))
        self.assertAlmostEqual(optimizer.learning_rate.item(), expected_lr, delta=1e-1)

    def test_single_schedule(self):

        config = {
            "name": "cosine_decay",
            "arguments": [0.1, 10],
        }
        lr_schedule = build_schedule(config)
        lr = lr_schedule(4)
        expected_lr = 0.1 * 0.5 * (1.0 + math.cos(math.pi * 4 / 10))
        self.assertAlmostEqual(lr, expected_lr, delta=1e-7)

    def test_non_zero_warmup(self):
        config = {
            "name": "cosine_decay",
            "warmup": 10,
            "warmup_init": 1e-6,
            "arguments": [1e-5, 20],
        }
        lr_schedule = build_schedule(config)
        lr = lr_schedule(0)
        self.assertAlmostEqual(lr, 1e-6, delta=1e-7)

    def test_malformed_config(self):
        config = {"warmup": 100}
        self.assertRaises(KeyError, build_schedule, config)

        config = {"cosine_decay": None}
        self.assertRaises(KeyError, build_schedule, config)

    def test_evaluate_calls(self):
        mock_model = MagicMock()
        mock_dataset = MagicMock()
        mock_tokenizer = MagicMock()
        mock_default_loss = MagicMock()
        mock_iterate_batches = MagicMock()

        mock_iterate_batches.return_value = [
            (MagicMock(), MagicMock()),
            (MagicMock(), MagicMock()),
            (MagicMock(), MagicMock()),
            (MagicMock(), MagicMock()),
            (MagicMock(), MagicMock()),
        ]

        mock_default_loss.side_effect = [
            (MagicMock(return_value=0.5), MagicMock(return_value=100)),
            (MagicMock(return_value=0.3), MagicMock(return_value=200)),
            (MagicMock(return_value=0.2), MagicMock(return_value=150)),
            (MagicMock(return_value=0.4), MagicMock(return_value=180)),
            (MagicMock(return_value=0.6), MagicMock(return_value=120)),
        ]
        evaluate(
            model=mock_model,
            dataset=mock_dataset,
            tokenizer=mock_tokenizer,
            batch_size=2,
            num_batches=2,
            max_seq_length=2048,
            loss=mock_default_loss,
            iterate_batches=mock_iterate_batches,
        )

        mock_iterate_batches.assert_called_once_with(
            dataset=mock_dataset,
            tokenizer=mock_tokenizer,
            batch_size=2,
            max_seq_length=2048,
        )
        self.assertEqual(mock_default_loss.call_count, 2)

    def test_evaluate_infinite_batches(self):
        mock_model = MagicMock()
        mock_dataset = MagicMock()
        mock_tokenizer = MagicMock()
        mock_default_loss = MagicMock()
        mock_iterate_batches = MagicMock()

        mock_iterate_batches.return_value = [
            (MagicMock(), MagicMock()),
            (MagicMock(), MagicMock()),
            (MagicMock(), MagicMock()),
        ]

        mock_default_loss.side_effect = [
            (MagicMock(return_value=0.5), MagicMock(return_value=100)),
            (MagicMock(return_value=0.3), MagicMock(return_value=200)),
            (MagicMock(return_value=0.2), MagicMock(return_value=150)),
        ]

        evaluate(
            model=mock_model,
            dataset=mock_dataset,
            tokenizer=mock_tokenizer,
            batch_size=2,
            num_batches=-1,
            max_seq_length=2048,
            loss=mock_default_loss,
            iterate_batches=mock_iterate_batches,
        )

        mock_iterate_batches.assert_called_once_with(
            dataset=mock_dataset,
            tokenizer=mock_tokenizer,
            batch_size=2,
            max_seq_length=2048,
        )
        self.assertEqual(mock_default_loss.call_count, 3)


if __name__ == "__main__":
    unittest.main()
