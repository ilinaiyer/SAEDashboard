import argparse
import gc
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, Set, Tuple, Optional, List
from dataclasses import asdict

import numpy as np
import torch
import wandb
import wandb.sdk
from matplotlib import colors
from sae_lens.sae import SAE  # type: ignore
from sae_lens.training.activations_store import ActivationsStore  # type: ignore
from tqdm import tqdm  # type: ignore
from transformer_lens import HookedTransformer  # type: ignore
from transformers import AutoModelForCausalLM  # type: ignore

from sae_dashboard.components_config import (
    ActsHistogramConfig,
    Column,
    FeatureTablesConfig,
    LogitsHistogramConfig,
    LogitsTableConfig,
    SequencesConfig,
)

# from sae_dashboard.data_writing_fns import save_feature_centric_vis
from sae_dashboard.layout import SaeVisLayoutConfig
from sae_dashboard.neuronpedia.neuronpedia_converter import NeuronpediaConverter
from sae_dashboard.neuronpedia.neuronpedia_runner_config import NeuronpediaRunnerConfig
from sae_dashboard.sae_vis_data import SaeVisConfig
from sae_dashboard.sae_vis_runner import SaeVisRunner
from sae_dashboard.utils_fns import has_duplicate_rows
from sae_dashboard.clt_layer_wrapper import CLTWrapperConfig

# set TOKENIZERS_PARALLELISM to false to avoid warnings
os.environ["TOKENIZERS_PARALLELISM"] = "false"
RUN_SETTINGS_FILE = "run_settings.json"
OUT_OF_RANGE_TOKEN = "<|outofrange|>"

BG_COLOR_MAP = colors.LinearSegmentedColormap.from_list(
    "bg_color_map", ["white", "darkorange"]
)


DEFAULT_FALLBACK_DEVICE = "cpu"

# TODO: add more anomalies here
HTML_ANOMALIES = {
    "âĢĶ": "—",
    "âĢĵ": "–",
    "âĢľ": """,
    "âĢĿ": """,
    "âĢĺ": "'",
    "âĢĻ": "'",
    "âĢĭ": " ",  # TODO: this is actually zero width space
    "Ġ": " ",
    "Ċ": "\n",
    "ĉ": "\t",
}


class NeuronpediaRunner:
    def __init__(
        self,
        cfg: NeuronpediaRunnerConfig,
    ):
        self.cfg = cfg

        # --- Validation ---
        flags = [
            self.cfg.use_transcoder,
            self.cfg.use_skip_transcoder,
            self.cfg.use_clt,
        ]
        if sum(flags) > 1:
            raise ValueError(
                "Only one of --use-transcoder, --use-skip-transcoder, or --use-clt can be set."
            )
        if self.cfg.use_clt and self.cfg.clt_layer_idx is None:
            raise ValueError("--clt-layer-idx must be specified when using --use-clt.")
        # --- End Validation ---

        # Get device defaults. But if we have overrides, then use those.
        device_count = 1
        # Set correct device, use multi-GPU if we have it
        if torch.backends.mps.is_available():
            self.cfg.sae_device = self.cfg.sae_device or "mps"
            self.cfg.model_device = self.cfg.model_device or "mps"
            self.cfg.model_n_devices = self.cfg.model_n_devices or 1
            self.cfg.activation_store_device = self.cfg.activation_store_device or "mps"
        elif torch.cuda.is_available():
            device_count = torch.cuda.device_count()
            if device_count > 1:
                self.cfg.sae_device = self.cfg.sae_device or f"cuda:{device_count - 1}"
                self.cfg.model_n_devices = self.cfg.model_n_devices or (
                    device_count - 1
                )
            else:
                self.cfg.sae_device = self.cfg.sae_device or "cuda"
            self.cfg.model_device = self.cfg.model_device or "cuda"
            self.cfg.sae_device = self.cfg.sae_device or "cuda"
            self.cfg.activation_store_device = (
                self.cfg.activation_store_device or "cuda"
            )
        else:
            self.cfg.sae_device = self.cfg.sae_device or "cpu"
            self.cfg.model_device = self.cfg.model_device or "cpu"
            self.cfg.model_n_devices = self.cfg.model_n_devices or 1
            self.cfg.activation_store_device = self.cfg.activation_store_device or "cpu"

        # Initialize SAE or Transcoder, defaulting to SAE dtype unless we override
        if self.cfg.use_skip_transcoder:
            # Dynamically import to avoid dependency issues when Transcoder isn't used
            try:
                from sae_lens.transcoder import SkipTranscoder  # type: ignore
            except ImportError as e:
                raise ImportError(
                    "SkipTranscoder class not found in sae_lens. Install a version of sae_lens that provides it or disable --use-skip-transcoder."
                ) from e
            LoaderClass = SkipTranscoder
            loader_kwargs = {}
            # TODO: Check if SkipTranscoder supports local loading via path= kwarg
            # if self.cfg.from_local_sae:
            #     loader_kwargs["path"] = self.cfg.sae_path
            # else:
            loader_kwargs["release"] = self.cfg.sae_set
            loader_kwargs["sae_id"] = self.cfg.sae_path

            self.sae, _, _ = LoaderClass.from_pretrained(  # type: ignore
                device=self.cfg.sae_device or DEFAULT_FALLBACK_DEVICE, **loader_kwargs
            )
            # SkipTranscoder doesn't directly support dtype override in from_pretrained, apply after
            if self.cfg.sae_dtype:
                try:
                    dtype_torch = getattr(torch, self.cfg.sae_dtype)
                    self.sae.to(dtype=dtype_torch)
                except AttributeError:
                    raise ValueError(f"Invalid sae_dtype: {self.cfg.sae_dtype}")

        elif self.cfg.use_transcoder:
            # Dynamically import to avoid dependency issues when Transcoder isn't used
            try:
                from sae_lens.transcoder import Transcoder  # type: ignore
            except ImportError as e:
                raise ImportError(
                    "Transcoder class not found in sae_lens. Install a version of sae_lens that provides Transcoder or disable --use-transcoder."
                ) from e
            LoaderClass = Transcoder

            if self.cfg.from_local_sae:
                # Transcoder might not have load_from_pretrained, use from_pretrained
                self.sae, _, _ = LoaderClass.from_pretrained(  # type: ignore
                    path=self.cfg.sae_path,
                    device=self.cfg.sae_device or DEFAULT_FALLBACK_DEVICE,
                    # dtype=self.cfg.sae_dtype if self.cfg.sae_dtype != "" else None, # Dtype applied after
                )
            else:
                self.sae, _, _ = LoaderClass.from_pretrained(  # type: ignore
                    release=self.cfg.sae_set,
                    sae_id=self.cfg.sae_path,
                    device=self.cfg.sae_device or DEFAULT_FALLBACK_DEVICE,
                )
            # Apply dtype override after loading for Transcoder as well
            if self.cfg.sae_dtype:
                try:
                    dtype_torch = getattr(torch, self.cfg.sae_dtype)
                    self.sae.to(dtype=dtype_torch)
                except AttributeError:
                    raise ValueError(f"Invalid sae_dtype: {self.cfg.sae_dtype}")
        elif self.cfg.use_clt:
            # Dynamically import CLT components only when needed
            try:
                from clt.models.clt import CrossLayerTranscoder  # type: ignore
                from clt.config.clt_config import CLTConfig  # type: ignore
            except ImportError as e:
                raise ImportError(
                    "CLT components (CrossLayerTranscoder, CLTConfig) not found. "
                    "Ensure the 'clt' package is installed and available." + str(e)
                ) from e

            if self.cfg.from_local_sae:
                # Assuming CLT config is saved as 'cfg.json' in the local path
                try:
                    clt_config_path = Path(self.cfg.sae_path) / "cfg.json"
                    if not clt_config_path.is_file():
                        raise FileNotFoundError(
                            f"CLT config file not found at {clt_config_path}"
                        )
                    clt_cfg = CLTConfig.from_json(clt_config_path)
                except Exception as e:
                    raise ValueError(
                        f"Failed to load CLT config from {clt_config_path}: {e}"
                    ) from e

                _temp_clt_for_debug = CrossLayerTranscoder(
                    config=clt_cfg,
                    process_group=None,
                    device=self.cfg.sae_device or DEFAULT_FALLBACK_DEVICE,
                )
                print(
                    "\n--- CLT Parameters BEFORE state_dict loading (Initial Random Values) ---"
                )
                encoder_module_temp = _temp_clt_for_debug.encoder_module.encoders[0]
                if (
                    hasattr(encoder_module_temp, "bias")
                    and encoder_module_temp.bias
                    and hasattr(encoder_module_temp, "bias_param")
                    and encoder_module_temp.bias_param is not None
                ):
                    print(
                        f"Norm of _temp_clt_for_debug.encoder_module.encoders[0].bias_param: {torch.norm(encoder_module_temp.bias_param.data).item()}"
                    )
                elif (
                    hasattr(encoder_module_temp, "bias")
                    and not encoder_module_temp.bias
                ):
                    print(
                        "_temp_clt_for_debug.encoder_module.encoders[0] has bias=False"
                    )
                else:
                    print(
                        "_temp_clt_for_debug.encoder_module.encoders[0].bias_param does not exist, is None, or bias flag is False"
                    )

                decoder_key_example = "0->0"
                decoder_module_temp = None
                if decoder_key_example in _temp_clt_for_debug.decoder_module.decoders:
                    decoder_module_temp = _temp_clt_for_debug.decoder_module.decoders[
                        decoder_key_example
                    ]

                if (
                    decoder_module_temp
                    and hasattr(decoder_module_temp, "bias")
                    and decoder_module_temp.bias
                    and hasattr(decoder_module_temp, "bias_param")
                    and decoder_module_temp.bias_param is not None
                ):
                    print(
                        f"Norm of _temp_clt_for_debug.decoder_module.decoders['{decoder_key_example}'].bias_param: {torch.norm(decoder_module_temp.bias_param.data).item()}"
                    )
                elif (
                    decoder_module_temp
                    and hasattr(decoder_module_temp, "bias")
                    and not decoder_module_temp.bias
                ):
                    print(
                        f"_temp_clt_for_debug.decoder_module.decoders['{decoder_key_example}'] has bias=False"
                    )
                else:
                    print(
                        f"_temp_clt_for_debug.decoder_module.decoders['{decoder_key_example}'].bias_param does not exist, is None, or bias flag is False, or module not found"
                    )
                del _temp_clt_for_debug
                print(
                    "---------------------------------------------------------------------\n"
                )

                self.clt = CrossLayerTranscoder(
                    config=clt_cfg,
                    process_group=None,
                    device=self.cfg.sae_device or DEFAULT_FALLBACK_DEVICE,
                )
                # ---> Load the state dictionary (supports .pt / .bin / .safetensors)
                # Determine which file to load. Preference order:
                # 1. Explicit filename provided via cfg.clt_weights_filename.
                # 2. First *.safetensors file found in sae_path directory.
                # 3. "model.safetensors", then "model.pt", then "model.bin".

                explicit_filename = (
                    self.cfg.clt_weights_filename
                    if hasattr(self.cfg, "clt_weights_filename")
                    else ""
                )

                candidate_paths: List[Path] = []
                if explicit_filename:
                    candidate_paths.append(Path(self.cfg.sae_path) / explicit_filename)

                # If no explicit filename or the file doesn't exist, search common patterns
                if not candidate_paths or not candidate_paths[0].is_file():
                    # Find any *.safetensors file in directory (common for CLT)
                    candidate_paths.extend(
                        sorted(Path(self.cfg.sae_path).glob("*.safetensors"))
                    )
                    # Add common legacy filenames
                    candidate_paths.append(
                        Path(self.cfg.sae_path) / "model.safetensors"
                    )
                    candidate_paths.append(Path(self.cfg.sae_path) / "model.pt")
                    candidate_paths.append(Path(self.cfg.sae_path) / "model.bin")

                # Pick the first existing path
                weights_path: Optional[Path] = None
                for cand in candidate_paths:
                    if cand.is_file():
                        weights_path = cand
                        break

                if weights_path is None:
                    print(
                        f"Warning: No CLT weights file found in {self.cfg.sae_path}. Expected one of: {', '.join(str(p) for p in candidate_paths)}. Weights are not loaded."
                    )
                else:
                    print(f"Loading CLT state dict from: {weights_path}")

                    # Choose loader based on file extension
                    if weights_path.suffix == ".safetensors":
                        try:
                            from safetensors.torch import load_file as safe_load_file
                        except ImportError as e:
                            raise ImportError(
                                "safetensors library is required to load .safetensors files. Install via `pip install safetensors`."
                            ) from e

                        raw_state_dict = safe_load_file(weights_path)
                    else:
                        raw_state_dict = torch.load(
                            weights_path, map_location=self.cfg.sae_device
                        )

                    state_dict = raw_state_dict
                    # state_dict = {
                    #     k.replace("_orig_mod.", ""): v
                    #     for k, v in raw_state_dict.items()
                    # }

                    print("\n--- Raw State Dict Parameter Norms (from model file) ---")
                    sample_keys_to_check = [
                        "encoder_module.encoders.0.weight",
                        "encoder_module.encoders.0.bias_param",
                        "decoder_module.decoders.0->0.weight",
                        "decoder_module.decoders.0->0.bias_param",
                    ]
                    for key_to_check in sample_keys_to_check:
                        if key_to_check in state_dict:
                            print(
                                f"Norm of state_dict['{key_to_check}']: {torch.norm(state_dict[key_to_check]).item()}"
                            )
                        else:
                            print(
                                f"Key '{key_to_check}' not found in processed state_dict."
                            )
                    print("--------------------------------------------------------\n")

                    # Load the processed state dict
                    self.clt.load_state_dict(state_dict)
                    print("CLT state dict loaded successfully.")

                    print("\n--- CLT Parameters AFTER state_dict loading ---")
                    idx_to_check_clt = 0
                    encoder_module_loaded = self.clt.encoder_module.encoders[
                        idx_to_check_clt
                    ]

                    if (
                        hasattr(encoder_module_loaded, "bias")
                        and encoder_module_loaded.bias
                        and hasattr(encoder_module_loaded, "bias_param")
                        and encoder_module_loaded.bias_param is not None
                    ):
                        loaded_bias_norm = torch.norm(
                            encoder_module_loaded.bias_param.data
                        ).item()
                        print(
                            f"Norm of self.clt.encoder_module.encoders[{idx_to_check_clt}].bias_param (after loading): {loaded_bias_norm}"
                        )
                        source_key = (
                            f"encoder_module.encoders.{idx_to_check_clt}.bias_param"
                        )
                        if source_key in state_dict:
                            print(
                                f"  (Compared to loaded state_dict['{source_key}'] norm: {torch.norm(state_dict[source_key]).item()})"
                            )
                    elif (
                        hasattr(encoder_module_loaded, "bias")
                        and not encoder_module_loaded.bias
                    ):
                        print(
                            f"self.clt.encoder_module.encoders[{idx_to_check_clt}] has bias=False after loading"
                        )
                    else:
                        print(
                            f"self.clt.encoder_module.encoders[{idx_to_check_clt}].bias_param is None, does not exist, or bias flag is False after loading"
                        )

                    decoder_key_to_check_clt = f"{idx_to_check_clt}->{idx_to_check_clt}"
                    decoder_module_loaded = None
                    if decoder_key_to_check_clt in self.clt.decoder_module.decoders:
                        decoder_module_loaded = self.clt.decoder_module.decoders[
                            decoder_key_to_check_clt
                        ]

                    if (
                        decoder_module_loaded
                        and hasattr(decoder_module_loaded, "bias")
                        and decoder_module_loaded.bias
                        and hasattr(decoder_module_loaded, "bias_param")
                        and decoder_module_loaded.bias_param is not None
                    ):
                        loaded_decoder_bias_norm = torch.norm(
                            decoder_module_loaded.bias_param.data
                        ).item()
                        print(
                            f"Norm of self.clt.decoder_module.decoders['{decoder_key_to_check_clt}'].bias_param (after loading): {loaded_decoder_bias_norm}"
                        )
                        source_decoder_key = f"decoder_module.decoders.{decoder_key_to_check_clt}.bias_param"
                        if source_decoder_key in state_dict:
                            print(
                                f"  (Compared to loaded state_dict['{source_decoder_key}'] norm: {torch.norm(state_dict[source_decoder_key]).item()})"
                            )
                    elif (
                        decoder_module_loaded
                        and hasattr(decoder_module_loaded, "bias")
                        and not decoder_module_loaded.bias
                    ):
                        print(
                            f"self.clt.decoder_module.decoders['{decoder_key_to_check_clt}'] has bias=False after loading"
                        )
                    else:
                        print(
                            f"self.clt.decoder_module.decoders['{decoder_key_to_check_clt}'].bias_param is None, does not exist, or bias flag is False, or module not found after loading"
                        )

                    if (
                        hasattr(encoder_module_loaded, "weight")
                        and encoder_module_loaded.weight is not None
                    ):
                        print(
                            f"Norm of self.clt.encoder_module.encoders[{idx_to_check_clt}].weight (after loading): {torch.norm(encoder_module_loaded.weight.data).item()}"
                        )
                    if (
                        decoder_module_loaded
                        and hasattr(decoder_module_loaded, "weight")
                        and decoder_module_loaded.weight is not None
                    ):
                        print(
                            f"Norm of self.clt.decoder_module.decoders['{decoder_key_to_check_clt}'].weight (after loading): {torch.norm(decoder_module_loaded.weight.data).item()}"
                        )
                    print("---------------------------------------------------\n")

                # This else block is misplaced and causes a syntax error.
                # else:
                #     print(
                #         f"Warning: CLT weights file not found at {weights_path}. Weights are not loaded."
                #     )
                #     # Potentially raise an error here if weights are mandatory
                #     # raise FileNotFoundError(f"CLT weights file not found at {weights_path}")

            else:
                # Placeholder for loading CLT from a HuggingFace-like release/id system
                # Adapt this if your CLT package provides a `from_pretrained` method
                # self.clt = CrossLayerTranscoder.from_pretrained(
                #     release=self.cfg.sae_set,  # Or appropriate mapping
                #     clt_id=self.cfg.sae_path,   # Or appropriate mapping
                #     device=self.cfg.sae_device or DEFAULT_FALLBACK_DEVICE,
                # )
                raise NotImplementedError(
                    "Loading CLT from non-local path (e.g., HF release) is not yet implemented."
                )

            # Apply dtype override if specified for CLT
            if self.cfg.clt_dtype:
                try:
                    dtype_torch = getattr(torch, self.cfg.clt_dtype)
                    self.clt.to(dtype=dtype_torch)
                    print(f"Overriding CLT dtype to {self.cfg.clt_dtype}")
                except AttributeError:
                    raise ValueError(f"Invalid clt_dtype: {self.cfg.clt_dtype}")
            elif hasattr(self.clt.config, "clt_dtype") and self.clt.config.clt_dtype:
                # Use CLT's configured dtype if override not specified
                self.cfg.clt_dtype = str(self.clt.config.clt_dtype).replace(
                    "torch.", ""
                )
                print(f"Using CLT configured dtype: {self.cfg.clt_dtype}")
            else:
                # Default if no override and no config dtype
                self.cfg.clt_dtype = "float32"
                print(f"CLT dtype not specified, defaulting to {self.cfg.clt_dtype}")

            # Instantiate the wrapper for the specific CLT layer
            from sae_dashboard.clt_layer_wrapper import CLTLayerWrapper  # Local import

            # Assert that clt_layer_idx is not None (already validated, but helps type checker)
            assert (
                self.cfg.clt_layer_idx is not None
            ), "CLT layer index should not be None here due to earlier validation."

            self.sae = CLTLayerWrapper(
                self.clt, self.cfg.clt_layer_idx, clt_model_dir_path=self.cfg.sae_path
            )
            print(f"Created CLTLayerWrapper for layer {self.cfg.clt_layer_idx}")

            print("\n--- CLTLayerWrapper Parameter Norms (self.sae) ---")
            clt_layer_idx_for_wrapper = self.cfg.clt_layer_idx
            assert (
                clt_layer_idx_for_wrapper is not None
            ), "clt_layer_idx should be set for CLTLayerWrapper"

            print(f"Norm of self.sae.W_enc: {torch.norm(self.sae.W_enc).item()}")
            clt_encoder_module_for_wrapper = self.clt.encoder_module.encoders[
                clt_layer_idx_for_wrapper
            ]
            if hasattr(clt_encoder_module_for_wrapper, "weight"):
                print(
                    f"  (Source self.clt.encoder_module.encoders[{clt_layer_idx_for_wrapper}].weight.t() norm: {torch.norm(clt_encoder_module_for_wrapper.weight.data.t()).item()})"
                )

            if self.sae.b_enc is not None:
                print(f"Norm of self.sae.b_enc: {torch.norm(self.sae.b_enc).item()}")
                if (
                    hasattr(clt_encoder_module_for_wrapper, "bias")
                    and clt_encoder_module_for_wrapper.bias
                    and hasattr(clt_encoder_module_for_wrapper, "bias_param")
                    and clt_encoder_module_for_wrapper.bias_param is not None
                ):
                    print(
                        f"  (Source self.clt.encoder_module.encoders[{clt_layer_idx_for_wrapper}].bias_param norm: {torch.norm(clt_encoder_module_for_wrapper.bias_param.data).item()})"
                    )
                elif (
                    hasattr(clt_encoder_module_for_wrapper, "bias")
                    and not clt_encoder_module_for_wrapper.bias
                ):
                    print(
                        f"  (Source self.clt.encoder_module.encoders[{clt_layer_idx_for_wrapper}] has bias=False)"
                    )
                else:
                    print(
                        f"  (Source self.clt.encoder_module.encoders[{clt_layer_idx_for_wrapper}].bias_param not found or bias flag false)"
                    )
            else:
                print("self.sae.b_enc is None")

            wrapper_decoder_key = (
                f"{clt_layer_idx_for_wrapper}->{clt_layer_idx_for_wrapper}"
            )
            clt_decoder_module_for_wrapper = None
            if wrapper_decoder_key in self.clt.decoder_module.decoders:
                clt_decoder_module_for_wrapper = self.clt.decoder_module.decoders[
                    wrapper_decoder_key
                ]

            print(f"Norm of self.sae.W_dec: {torch.norm(self.sae.W_dec).item()}")
            if clt_decoder_module_for_wrapper and hasattr(
                clt_decoder_module_for_wrapper, "weight"
            ):
                print(
                    f"  (Source self.clt.decoder_module.decoders['{wrapper_decoder_key}'].weight.t() norm: {torch.norm(clt_decoder_module_for_wrapper.weight.data.t()).item()})"
                )

            if self.sae.b_dec is not None:
                print(f"Norm of self.sae.b_dec: {torch.norm(self.sae.b_dec).item()}")
                if (
                    clt_decoder_module_for_wrapper
                    and hasattr(clt_decoder_module_for_wrapper, "bias")
                    and clt_decoder_module_for_wrapper.bias
                    and hasattr(clt_decoder_module_for_wrapper, "bias_param")
                    and clt_decoder_module_for_wrapper.bias_param is not None
                ):
                    print(
                        f"  (Source self.clt.decoder_module.decoders['{wrapper_decoder_key}'].bias_param norm: {torch.norm(clt_decoder_module_for_wrapper.bias_param.data).item()})"
                    )
                elif (
                    clt_decoder_module_for_wrapper
                    and hasattr(clt_decoder_module_for_wrapper, "bias")
                    and not clt_decoder_module_for_wrapper.bias
                ):
                    print(
                        f"  (Source self.clt.decoder_module.decoders['{wrapper_decoder_key}'] has bias=False)"
                    )
                else:
                    print(
                        f"  (Source self.clt.decoder_module.decoders['{wrapper_decoder_key}'].bias_param not found, or decoder module not found, or bias flag false)"
                    )
            else:
                print("self.sae.b_dec is None")
            print("-----------------------------------------------------\n")

        else:
            LoaderClass = SAE
            if self.cfg.from_local_sae:
                self.sae = LoaderClass.load_from_pretrained(
                    path=self.cfg.sae_path,
                    device=self.cfg.sae_device or DEFAULT_FALLBACK_DEVICE,
                    dtype=self.cfg.sae_dtype if self.cfg.sae_dtype != "" else None,
                )
            else:
                self.sae, _, _ = LoaderClass.from_pretrained(
                    release=self.cfg.sae_set,
                    sae_id=self.cfg.sae_path,
                    device=self.cfg.sae_device or DEFAULT_FALLBACK_DEVICE,
                )
            # Apply dtype override for standard SAE if specified
            if self.cfg.sae_dtype and not self.cfg.from_local_sae:
                # load_from_pretrained handles dtype for local, from_pretrained needs manual application
                try:
                    dtype_torch = getattr(torch, self.cfg.sae_dtype)
                    self.sae.to(dtype=dtype_torch)
                except AttributeError:
                    raise ValueError(f"Invalid sae_dtype: {self.cfg.sae_dtype}")

        # If we didn't override dtype, then use the SAE's dtype
        if self.cfg.sae_dtype == "":
            print(f"Using SAE configured dtype: {self.sae.cfg.dtype}")
            self.cfg.sae_dtype = self.sae.cfg.dtype
        else:
            print(f"Overriding sae dtype to {self.cfg.sae_dtype}")

        if self.cfg.model_dtype == "":
            self.cfg.model_dtype = "float32"

        # double sure this works
        self.sae.to(self.cfg.sae_device or DEFAULT_FALLBACK_DEVICE)
        self.sae.cfg.device = self.cfg.sae_device or DEFAULT_FALLBACK_DEVICE

        if self.cfg.huggingface_dataset_path == "":
            self.cfg.huggingface_dataset_path = self.sae.cfg.dataset_path

        print(f"Device Count: {device_count}")
        print(f"SAE Device: {self.cfg.sae_device}")
        print(f"Model Device: {self.cfg.model_device}")
        print(f"Model Num Devices: {self.cfg.model_n_devices}")
        print(f"Activation Store Device: {self.cfg.activation_store_device}")
        print(f"Dataset Path: {self.cfg.huggingface_dataset_path}")
        print(f"Forward Pass size: {self.cfg.n_tokens_in_prompt}")

        # number of tokens
        n_tokens_total = self.cfg.n_prompts_total * self.cfg.n_tokens_in_prompt
        print(f"Total number of tokens: {n_tokens_total}")
        print(f"Total number of contexts (prompts): {self.cfg.n_prompts_total}")

        # --- Determine Model Config ---
        model_cfg = self.sae.cfg

        if isinstance(model_cfg, CLTWrapperConfig):
            import dataclasses

            sae_cfg_json = dataclasses.asdict(model_cfg)
        elif hasattr(model_cfg, "to_dict"):
            sae_cfg_json = model_cfg.to_dict()
        else:
            try:
                sae_cfg_json = vars(model_cfg)
            except TypeError:
                sae_cfg_json = {}

        sae_from_pretrained_kwargs = sae_cfg_json.get(
            "model_from_pretrained_kwargs", {}
        )
        print("SAE/Wrapper Config:")
        import pprint

        pprint.pprint(sae_cfg_json)
        if sae_from_pretrained_kwargs:
            print("SAE/Wrapper has from_pretrained_kwargs", sae_from_pretrained_kwargs)
        else:
            print(
                "SAE/Wrapper does not have from_pretrained_kwargs. Standard TransformerLens Loading"
            )
        # --- End Determine Model Config ---

        # --- Set Config Values based on Runner Config ---
        current_dataset_path = getattr(model_cfg, "dataset_path", None)
        if not current_dataset_path:
            model_cfg.dataset_path = self.cfg.huggingface_dataset_path
            print(f"Set model_cfg dataset_path: {model_cfg.dataset_path}")

        current_context_size = getattr(model_cfg, "context_size", None)
        if current_context_size is None:
            model_cfg.context_size = self.cfg.n_tokens_in_prompt
            print(f"Set model_cfg context_size: {model_cfg.context_size}")

        # Prepend_bos should now exist on model_cfg due to CLTWrapperConfig update
        # No need to set it here unless we want to explicitly override based on runner cfg
        # --- End Set Config Values ---

        # --- Prepare Base Model ---
        # if hasattr(self.sae, "fold_W_dec_norm") and callable(self.sae.fold_W_dec_norm):
        #     self.sae.fold_W_dec_norm()
        # else:
        #     print("Skipping fold_W_dec_norm: Method not found")
        print("\n--- Skipping fold_W_dec_norm as per user request. ---\n")

        self.model_id = getattr(model_cfg, "model_name", None) or getattr(
            self.cfg, "model_id", None
        )
        if not self.model_id:
            raise ValueError("Could not determine model_id")
        self.cfg.model_id = self.model_id
        self.layer = model_cfg.hook_layer
        self.cfg.layer = self.layer

        print(f"SAE/Wrapper DType: {model_cfg.dtype}")
        print(f"Base Model DType: {self.cfg.model_dtype}")

        hf_model = None
        if self.cfg.hf_model_path:
            hf_model = AutoModelForCausalLM.from_pretrained(self.cfg.hf_model_path)

        print(f"Loading base model: {self.model_id}...")
        self.model = HookedTransformer.from_pretrained(
            model_name=self.model_id,
            device=self.cfg.model_device,
            n_devices=self.cfg.model_n_devices or 1,
            hf_model=hf_model,
            dtype=self.cfg.model_dtype,
            **sae_from_pretrained_kwargs,
        )
        print(f"Base model {self.model_id} loaded successfully.")
        # --- End Prepare Base Model ---

        # --- Final Setup (Activations Store, Output Dir, Vocab) ---
        # Ensure MLP-in hooks are computed if needed
        hook_name_to_check = getattr(model_cfg, "hook_name", "")
        if (
            self.cfg.use_transcoder
            or self.cfg.use_skip_transcoder
            or self.cfg.use_clt
            or "hook_mlp_in" in hook_name_to_check
        ) and hasattr(self.model, "set_use_hook_mlp_in"):
            print("Setting use_hook_mlp_in=True on the base model.")
            self.model.set_use_hook_mlp_in(True)

        # Initialize Activations Store
        print("Initializing ActivationsStore...")
        self.activations_store = ActivationsStore.from_sae(
            model=self.model,
            sae=self.sae,
            streaming=True,
            store_batch_size_prompts=self.cfg.n_prompts_in_forward_pass,
            n_batches_in_buffer=16,
            device=self.cfg.activation_store_device or "cpu",
        )
        # Ensure ActivationsStore context size uses the value from model_cfg
        # which might have been updated from runner cfg
        self.activations_store.context_size = model_cfg.context_size

        self.cached_activations_dir = Path(
            f"./cached_activations/{self.model_id}_{self.cfg.sae_set}_{hook_name_to_check}_{model_cfg.d_sae}width_{self.cfg.n_prompts_total}prompts"
        )

        # This override seems redundant if already set via model_cfg, remove or clarify?
        # if self.cfg.n_tokens_in_prompt is not None:
        #     self.activations_store.context_size = self.cfg.n_tokens_in_prompt

        self.np_sae_id_suffix = self.cfg.np_sae_id_suffix

        if not os.path.exists(cfg.outputs_dir):
            os.makedirs(cfg.outputs_dir)
        self.cfg.outputs_dir = self.create_output_directory()

        self.vocab_dict = self.get_vocab_dict()

    def create_output_directory(self) -> str:
        """
        Creates the output directory for storing generated features.

        Returns:
            Path: The path to the created output directory.
        """
        outputs_subdir = f"{self.model_id}_{self.cfg.sae_set}_{self.sae.cfg.hook_name}_{self.sae.cfg.d_sae}"
        if self.np_sae_id_suffix is not None:
            outputs_subdir += f"_{self.np_sae_id_suffix}"
        outputs_dir = Path(self.cfg.outputs_dir).joinpath(outputs_subdir)
        if outputs_dir.exists() and outputs_dir.is_file():
            raise ValueError(
                f"Error: Output directory {outputs_dir.as_posix()} exists and is a file."
            )
        outputs_dir.mkdir(parents=True, exist_ok=True)
        return str(outputs_dir)

    def hash_tensor(self, tensor: torch.Tensor) -> Tuple[int, ...]:
        return tuple(tensor.cpu().numpy().flatten().tolist())

    def generate_tokens(
        self,
        activations_store: ActivationsStore,
        n_prompts: int = 4096 * 6,
    ) -> torch.Tensor:
        all_tokens_list = []
        unique_sequences: Set[Tuple[int, ...]] = set()
        pbar = tqdm(range(n_prompts // activations_store.store_batch_size_prompts))

        for _ in pbar:
            batch_tokens = activations_store.get_batch_tokens()
            if self.cfg.shuffle_tokens:
                batch_tokens = batch_tokens[torch.randperm(batch_tokens.shape[0])]

            # Check for duplicates and only add unique sequences
            for seq in batch_tokens:
                seq_hash = self.hash_tensor(seq)
                if seq_hash not in unique_sequences:
                    unique_sequences.add(seq_hash)
                    all_tokens_list.append(seq.unsqueeze(0))

            # Early exit if we've collected enough unique sequences
            if len(all_tokens_list) >= n_prompts:
                break

        all_tokens = torch.cat(all_tokens_list, dim=0)[:n_prompts]
        if self.cfg.shuffle_tokens:
            all_tokens = all_tokens[torch.randperm(all_tokens.shape[0])]

        return all_tokens

    def add_prefix_suffix_to_tokens(self, tokens: torch.Tensor) -> torch.Tensor:
        original_length = tokens.shape[1]
        bos_tokens = tokens[:, 0]  # might not be if sae.cfg.prepend_bos is False
        prefix_length = len(self.cfg.prefix_tokens) if self.cfg.prefix_tokens else 0
        suffix_length = len(self.cfg.suffix_tokens) if self.cfg.suffix_tokens else 0

        # return tokens if no prefix or suffix
        if self.cfg.prefix_tokens is None and self.cfg.suffix_tokens is None:
            return tokens

        # Calculate how many tokens to keep from the original
        keep_length = original_length - prefix_length - suffix_length

        if keep_length <= 0:
            raise ValueError("Prefix and suffix are too long for the given tokens.")

        # Trim original tokens
        tokens = tokens[:, : keep_length - self.sae.cfg.prepend_bos]

        if self.cfg.prefix_tokens:
            prefix = torch.tensor(self.cfg.prefix_tokens).to(tokens.device)
            prefix_repeated = prefix.unsqueeze(0).repeat(tokens.shape[0], 1)
            # if sae.cfg.prepend_bos, then add that before the suffix
            if self.sae.cfg.prepend_bos:
                bos = bos_tokens.unsqueeze(1)
                prefix_repeated = torch.cat([bos, prefix_repeated], dim=1)
            tokens = torch.cat([prefix_repeated, tokens], dim=1)

        if self.cfg.suffix_tokens:
            suffix = torch.tensor(self.cfg.suffix_tokens).to(tokens.device)
            suffix_repeated = suffix.unsqueeze(0).repeat(tokens.shape[0], 1)
            tokens = torch.cat([tokens, suffix_repeated], dim=1)

        # assert length hasn't changed
        assert tokens.shape[1] == original_length
        return tokens

    def get_alive_features(self) -> list[int]:
        # skip sparsity
        target_feature_indexes = list(range(self.sae.cfg.d_sae))
        print("Warning: Sparsity option is not implemented, running all features.")
        # TODO: post-refactor the load_sparsity no longer exists
        # if self.cfg.sparsity_threshold == 1:
        #     print("Skipping sparsity because sparsity_threshold was set to 1")
        #     target_feature_indexes = list(range(self.sae.cfg.d_sae))
        # else:
        #     # if we have feature sparsity, then use it to only generate outputs for non-dead features
        #     self.target_feature_indexes: list[int] = []
        #     # sparsity = load_sparsity(self.cfg.sae_path)
        #     # convert sparsity to logged sparsity if it's not
        #     # TODO: standardize the sparsity file format
        #     # if len(sparsity) > 0 and sparsity[0] >= 0:
        #     #     sparsity = torch.log10(sparsity + 1e-10)
        #     # target_feature_indexes = (
        #     #     (sparsity > self.cfg.sparsity_threshold)
        #     #     .nonzero(as_tuple=True)[0]
        #     #     .tolist()
        #     # )
        return target_feature_indexes

    def get_feature_batches(self):

        # divide into batches
        feature_idx = torch.tensor(self.target_feature_indexes)
        n_subarrays = np.ceil(len(feature_idx) / self.cfg.n_features_at_a_time).astype(
            int
        )
        feature_idx = np.array_split(feature_idx, n_subarrays)
        feature_idx = [x.tolist() for x in feature_idx]

        return feature_idx

    def record_skipped_features(self):
        # write dead into file so we can create them as dead in Neuronpedia
        skipped_indexes = set(range(self.n_features)) - set(self.target_feature_indexes)
        skipped_indexes_json = json.dumps(
            {
                "model_id": self.model_id,
                "layer": str(self.layer),
                "sae_set": self.cfg.sae_set,
                "log_sparsity": self.cfg.sparsity_threshold,
                "skipped_indexes": list(skipped_indexes),
            }
        )
        with open(f"{self.cfg.outputs_dir}/skipped_indexes.json", "w") as f:
            f.write(skipped_indexes_json)

    def get_tokens(self):

        tokens_file = f"{self.cfg.outputs_dir}/tokens_{self.cfg.n_prompts_total}.pt"
        if os.path.isfile(tokens_file):
            print("Tokens exist, loading them.")
            tokens = torch.load(tokens_file)
        else:
            print("Tokens don't exist, making them.")
            tokens = self.generate_tokens(
                self.activations_store,
                self.cfg.n_prompts_total,
            )
            torch.save(
                tokens,
                tokens_file,
            )

        assert not has_duplicate_rows(tokens), "Duplicate rows in tokens"

        return tokens

    def get_vocab_dict(self) -> Dict[int, str]:
        # get vocab
        vocab_dict = self.model.tokenizer.vocab  # type: ignore
        new_vocab_dict = {}
        # Replace substrings in the keys of vocab_dict using HTML_ANOMALIES
        for k, v in vocab_dict.items():  # type: ignore
            modified_key = k
            for anomaly in HTML_ANOMALIES:
                modified_key = modified_key.replace(anomaly, HTML_ANOMALIES[anomaly])
            new_vocab_dict[v] = modified_key
        vocab_dict = new_vocab_dict
        # pad with blank tokens to the actual vocab size
        for i in range(len(vocab_dict), self.model.cfg.d_vocab):
            vocab_dict[i] = OUT_OF_RANGE_TOKEN
        return vocab_dict

    # TODO: make this function simpler
    def run(self):

        run_settings_path = self.cfg.outputs_dir + "/" + RUN_SETTINGS_FILE
        run_settings = self.cfg.__dict__
        with open(run_settings_path, "w") as f:
            json.dump(run_settings, f, indent=4)

        wandb_cfg = self.cfg.__dict__
        wandb_cfg["sae_cfg"] = asdict(self.sae.cfg)

        current_time = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        set_name = (
            self.cfg.sae_set if self.cfg.np_set_name is None else self.cfg.np_set_name
        )
        if self.cfg.use_wandb:
            wandb.init(
                project="sae-dashboard-generation",
                name=f"{self.model_id}_{set_name}_{self.sae.cfg.hook_name}_{current_time}",
                save_code=True,
                mode="online",
                config=wandb_cfg,
            )

        self.n_features = self.sae.cfg.d_sae
        assert self.n_features is not None

        self.target_feature_indexes = self.get_alive_features()

        feature_idx = self.get_feature_batches()
        if self.cfg.start_batch >= len(feature_idx):
            print(
                f"Start batch {self.cfg.start_batch} is greater than number of batches {len(feature_idx)}, exiting"
            )
            exit()

        self.record_skipped_features()
        tokens = self.get_tokens()
        tokens = self.add_prefix_suffix_to_tokens(tokens)

        del self.activations_store

        with torch.no_grad():
            for feature_batch_count, features_to_process in tqdm(
                enumerate(feature_idx)
            ):

                if feature_batch_count < self.cfg.start_batch:
                    feature_batch_count = feature_batch_count + 1
                    continue
                if (
                    self.cfg.end_batch is not None
                    and feature_batch_count > self.cfg.end_batch
                ):
                    feature_batch_count = feature_batch_count + 1
                    continue

                output_file = f"{self.cfg.outputs_dir}/batch-{feature_batch_count}.json"
                # if output_file exists, skip
                if os.path.isfile(output_file):
                    logline = f"\n++++++++++ Skipping Batch #{feature_batch_count} output. File exists: {output_file} ++++++++++\n"
                    print(logline)
                    continue

                print(f"========== Running Batch #{feature_batch_count} ==========")

                layout = SaeVisLayoutConfig(
                    columns=[
                        Column(
                            SequencesConfig(
                                stack_mode="stack-all",
                                buffer=None,  # type: ignore
                                compute_buffer=True,
                                n_quantiles=self.cfg.n_quantiles,
                                top_acts_group_size=self.cfg.top_acts_group_size,
                                quantile_group_size=self.cfg.quantile_group_size,
                            ),
                            ActsHistogramConfig(),
                            LogitsHistogramConfig(),
                            LogitsTableConfig(),
                            FeatureTablesConfig(n_rows=3),
                        )
                    ]
                )

                feature_vis_config_gpt = SaeVisConfig(
                    hook_point=self.sae.cfg.hook_name,
                    features=features_to_process,
                    minibatch_size_features=self.cfg.n_features_at_a_time,
                    minibatch_size_tokens=self.cfg.n_prompts_in_forward_pass,
                    quantile_feature_batch_size=self.cfg.quantile_feature_batch_size,
                    verbose=True,
                    device=self.cfg.sae_device or DEFAULT_FALLBACK_DEVICE,
                    feature_centric_layout=layout,
                    perform_ablation_experiments=False,
                    dtype=self.cfg.sae_dtype,
                    cache_dir=self.cached_activations_dir,
                    ignore_tokens={self.model.tokenizer.pad_token_id, self.model.tokenizer.bos_token_id, self.model.tokenizer.eos_token_id},  # type: ignore
                    ignore_positions=self.cfg.ignore_positions or [],
                    use_dfa=self.cfg.use_dfa,
                )

                feature_data = SaeVisRunner(feature_vis_config_gpt).run(
                    encoder=self.sae,
                    model=self.model,
                    tokens=tokens,
                )

                # if feature_batch_count == 0:
                #     html_save_path = (
                #         f"{self.cfg.outputs_dir}/batch-{feature_batch_count}.html"
                #     )
                #     save_feature_centric_vis(
                #         sae_vis_data=feature_data,
                #         filename=html_save_path,
                #         # use only the first 10 features for the dashboard
                #         include_only=features_to_process[
                #             : max(10, len(features_to_process))
                #         ],
                #     )

                #     if self.cfg.use_wandb:
                #         wandb.log(
                #             data={
                #                 "batch": feature_batch_count,
                #                 "dashboard": wandb.Html(open(html_save_path)),
                #             },
                #             step=feature_batch_count,
                #         )
                self.cfg.model_id = self.model_id
                self.cfg.layer = self.layer
                json_object = NeuronpediaConverter.convert_to_np_json(
                    self.model, feature_data, self.cfg, self.vocab_dict
                )
                with open(
                    output_file,
                    "w",
                ) as f:
                    f.write(json_object)
                print(f"Output written to {output_file}")

                logline = f"\n========== Completed Batch #{feature_batch_count} output: {output_file} ==========\n"
                if self.cfg.use_wandb:
                    wandb.log(
                        {"batch": feature_batch_count},
                        step=feature_batch_count,
                    )
                # Clean up after each batch
                del feature_data
                gc.collect()
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
        if self.cfg.use_wandb:
            wandb.sdk.finish()


def main():
    parser = argparse.ArgumentParser(description="Run Neuronpedia feature generation")
    parser.add_argument("--sae-set", required=True, help="SAE set name")
    parser.add_argument("--sae-path", required=True, help="Path to SAE")
    parser.add_argument("--np-set-name", required=True, help="Neuronpedia set name")
    parser.add_argument(
        "--np-sae-id-suffix",
        required=False,
        help="Additional suffix on Neuronpedia for the SAE ID. Goes after the SAE Set like so: __[np-sae-id-suffix]. Used for additional l0s, training steps, etc.",
    )
    parser.add_argument(
        "--dataset-path", required=True, help="HuggingFace dataset path"
    )
    parser.add_argument(
        "--sae_dtype", default="float32", help="Data type for sae computations"
    )
    parser.add_argument(
        "--model_dtype", default="float32", help="Data type for model computations"
    )
    parser.add_argument(
        "--output-dir", default="neuronpedia_outputs/", help="Output directory"
    )
    parser.add_argument(
        "--sparsity-threshold", type=int, default=1, help="Sparsity threshold"
    )
    parser.add_argument("--n-prompts", type=int, default=128, help="Number of prompts")
    parser.add_argument(
        "--n-tokens-in-prompt", type=int, default=128, help="Number of tokens in prompt"
    )
    parser.add_argument(
        "--n-prompts-in-forward-pass",
        type=int,
        default=128,
        help="Number of prompts in forward pass",
    )
    parser.add_argument(
        "--n-features-per-batch",
        type=int,
        default=2,
        help="Number of features per batch",
    )
    parser.add_argument(
        "--start-batch", type=int, default=0, help="Starting batch number"
    )
    parser.add_argument(
        "--end-batch", type=int, default=None, help="Ending batch number"
    )
    parser.add_argument(
        "--use-wandb", action="store_true", help="Use Weights & Biases for logging"
    )
    parser.add_argument(
        "--from-local-sae", action="store_true", help="Load SAE from local path"
    )
    parser.add_argument(
        "--hf-model-path",
        type=str,
        default=None,
        help="Optional: Path to custom HuggingFace model to use instead of default weights",
    )
    parser.add_argument(
        "--use-transcoder",
        action="store_true",
        help="If set, load a Transcoder instead of a standard SAE",
    )
    parser.add_argument(
        "--use-skip-transcoder",
        action="store_true",
        help="If set, load a SkipTranscoder instead of a Transcoder/SAE",
    )
    parser.add_argument(
        "--use-clt",
        action="store_true",
        help="If set, load a CrossLayerTranscoder instead of a standard SAE/Transcoder",
    )
    parser.add_argument(
        "--clt-layer-idx",
        type=int,
        default=None,
        help="Layer index to use for CLT encoder (required if --use-clt)",
    )
    parser.add_argument(
        "--clt-dtype",
        type=str,
        default="",
        help="Optional override for CLT data type (e.g., 'float16')",
    )
    parser.add_argument(
        "--clt-weights-filename",
        type=str,
        default="",
        help="Filename of the CLT weights file (supports .safetensors / .pt). If omitted, script will search for a suitable file automatically.",
    )

    args = parser.parse_args()

    cfg = NeuronpediaRunnerConfig(
        sae_set=args.sae_set,
        sae_path=args.sae_path,
        np_set_name=args.np_set_name,
        np_sae_id_suffix=args.np_sae_id_suffix,
        from_local_sae=args.from_local_sae,
        huggingface_dataset_path=args.dataset_path,
        sae_dtype=args.sae_dtype,
        model_dtype=args.model_dtype,
        outputs_dir=args.output_dir,
        sparsity_threshold=args.sparsity_threshold,
        n_prompts_total=args.n_prompts,
        n_tokens_in_prompt=args.n_tokens_in_prompt,
        n_prompts_in_forward_pass=args.n_prompts_in_forward_pass,
        n_features_at_a_time=args.n_features_per_batch,
        start_batch=args.start_batch,
        end_batch=args.end_batch,
        use_wandb=args.use_wandb,
        hf_model_path=args.hf_model_path,
        use_transcoder=args.use_transcoder,
        use_skip_transcoder=args.use_skip_transcoder,
        use_clt=args.use_clt,
        clt_layer_idx=args.clt_layer_idx,
        clt_dtype=args.clt_dtype,
        clt_weights_filename=args.clt_weights_filename,
    )

    runner = NeuronpediaRunner(cfg)
    runner.run()


if __name__ == "__main__":
    main()
