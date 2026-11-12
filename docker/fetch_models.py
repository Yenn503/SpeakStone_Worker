"""Fetch the files the voice engine loads into one folder: <root>/IndexTTS-2 (the model) and <root>/hf/hub (four
extra files the engine otherwise fetches on its first job). Files already there are skipped, a part-finished file
carries on. Used when the image is built, and at first start of the slim image.

Usage: python fetch_models.py <root>
"""
import os
import sys
from pathlib import Path

MODEL_REPO = "IndexTeam/IndexTTS-2"
MODEL_FILES = ("config.yaml", "gpt.pth", "s2mel.pth", "bpe.model")
SKIP = ["qwen*/*", "qwen*"]                      # a text model the worker never loads (about 1.2 GB)
EXTRA_SNAPSHOTS = (("facebook/w2v-bert-2.0", ["config.json", "preprocessor_config.json", "model.safetensors"]),
                   ("nvidia/bigvgan_v2_22khz_80band_256x", ["config.json", "bigvgan_generator.pt"]))
EXTRA_FILES = (("amphion/MaskGCT", "semantic_codec/model.safetensors"),
               ("funasr/campplus", "campplus_cn_common.bin"))
EXTRA_NEED = {"models--facebook--w2v-bert-2.0": "model.safetensors",
              "models--nvidia--bigvgan_v2_22khz_80band_256x": "bigvgan_generator.pt",
              "models--amphion--MaskGCT": "semantic_codec/model.safetensors",
              "models--funasr--campplus": "campplus_cn_common.bin"}


def model_complete(model_dir):
    return all((model_dir / f).is_file() and (model_dir / f).stat().st_size > 0 for f in MODEL_FILES)


def extras_complete(hub):
    for repo, name in EXTRA_NEED.items():
        snaps = hub / repo / "snapshots"
        if not snaps.is_dir() or not any((s / name).exists() for s in snaps.iterdir()):
            return False
        if any((hub / repo / "blobs").glob("*.incomplete")):
            return False
    return True


def complete(root):
    root = Path(root)
    return model_complete(root / "IndexTTS-2") and extras_complete(root / "hf" / "hub")


def main(root):
    root = Path(root).resolve()
    model_dir, hf_home = root / "IndexTTS-2", root / "hf"
    # must be set before huggingface_hub is imported: it reads them once
    os.environ["HF_HOME"] = str(hf_home)
    os.environ["HF_HUB_CACHE"] = str(hf_home / "hub")
    os.environ.setdefault("HF_XET_HIGH_PERFORMANCE", "1")      # use the whole connection: this is a one-off bulk fetch
    from huggingface_hub import hf_hub_download, snapshot_download
    if not model_complete(model_dir):
        snapshot_download(MODEL_REPO, local_dir=str(model_dir), ignore_patterns=SKIP)
    for repo, pats in EXTRA_SNAPSHOTS:
        snapshot_download(repo, allow_patterns=pats)
    for repo, name in EXTRA_FILES:
        hf_hub_download(repo, filename=name)
    if not complete(root):
        print("model files are not complete after the download", file=sys.stderr)
        return 1
    print(f"model files ready in {root}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
