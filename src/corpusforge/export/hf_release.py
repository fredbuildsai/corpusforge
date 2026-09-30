"""Stage: publish a GGUF model to Hugging Face Hub, with a model card.

Per https://huggingface.co/docs/hub/model-cards: a YAML metadata block (base_model, license,
language, tags) followed by Markdown sections covering model details, intended use, training
data/procedure, and limitations. Training data provenance comes from `LICENSE_STATUS.json`
(written by the host project's export step) and the dataset's own `.attribution.json` files -
see `export.licensing` for how those are built and enforced. The publish step itself refuses to
run unless `LICENSE_STATUS.json` says `shareable: true` - enforced by the host's `push-to-hub` command.

Everything domain-specific in the card (tags, intended use, limitations) comes from a `ModelCardInfo` the host
supplies; the defaults are deliberately generic.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ModelCardInfo:
    """The domain-specific parts of a model card. Defaults are generic and safe for any domain."""

    tags: tuple[str, ...] = ()
    intended_use: str = (
        "This is a proof-of-concept fine-tune, not a production or clinical-grade system - verify factual "
        "claims against primary literature before relying on them."
    )
    limitations: tuple[str, ...] = (
        "Trained on a small step count as a proof of concept, not to convergence - expect uneven coverage of "
        "the underlying literature and occasional generic (non-grounded) answers.",
        "Treat outputs as a draft requiring expert review, not an authoritative source.",
    )
    fine_tuning_method: str = (
        "LoRA via [Unsloth](https://github.com/unslothai/unsloth) (MLX backend, Apple Silicon)"
    )


MODEL_CARD_TEMPLATE = """\
---
base_model: {base_model}
license: {license}
tags:
  - unsloth
  - lora
  - gguf
{extra_tags}---

# {repo_name}

## Model Details

- **Base model:** [{base_model}](https://huggingface.co/{base_model})
- **Fine-tuning method:** {fine_tuning_method}
- **Training stage(s):** {stage_description}
- **Format:** GGUF, quantized {quantization}
- **License:** {license}

## Intended Use

{intended_use}

## Training Data

Trained on {total_rows} example(s) drawn from {num_sources} open-licensed source document(s) \
(CC0 / CC-BY / public-domain only - see `ATTRIBUTION.json` in this repo for the complete \
per-document DOI/license breakdown). No copyrighted or unclear-license material is included: \
every source was screened against an open-license allowlist before export, and any document that \
failed that check was excluded.

## Training Procedure

{training_procedure}

## Bias, Risks, and Limitations

{limitations}
"""


def attribution_summary(export_dir: Path) -> dict[str, Any]:
    """Union every `*.attribution.json` in `export_dir` into one DOI/doc_id -> source-info map,
    for the ATTRIBUTION.json shipped alongside the model and the source count in the card."""
    combined: dict[str, Any] = {}
    for path in sorted(export_dir.glob("*.attribution.json")):
        with open(path, encoding="utf-8") as f:
            manifest = json.load(f)
        for key, entry in manifest.items():
            existing = combined.setdefault(key, {"doc_id": entry["doc_id"], "license": entry["license"],
                                                   "title": entry["title"], "rows": 0})
            existing["rows"] += len(entry["chunks"])
    return combined


def build_model_card(*, repo_name: str, base_model: str, license_id: str, quantization: str,
                     training_provenance: dict[str, Any], attribution: dict[str, Any],
                     card: ModelCardInfo | None = None) -> str:
    card = card or ModelCardInfo()
    total_rows = sum(entry["rows"] for entry in attribution.values())
    stage = training_provenance.get("stage", "unknown")
    from_adapter = training_provenance.get("from_adapter")
    stage_description = f"{stage.upper()}" + (f" (continued from a prior {from_adapter} adapter)"
                                                if from_adapter else "")
    training_procedure = (
        f"- **Stage:** {stage_description}\n"
        f"- **Dataset:** `{Path(training_provenance.get('dataset', 'unknown')).name}`\n"
        f"- **Config:** `configs/{training_provenance.get('config', 'unknown')}.yaml`"
    )
    return MODEL_CARD_TEMPLATE.format(
        repo_name=repo_name, base_model=base_model, license=license_id, quantization=quantization,
        stage_description=stage_description, total_rows=total_rows, num_sources=len(attribution),
        training_procedure=training_procedure,
        extra_tags="".join(f"  - {tag}\n" for tag in card.tags), intended_use=card.intended_use,
        limitations="\n".join(f"- {item}" for item in card.limitations),
        fine_tuning_method=card.fine_tuning_method,
    )


def push_gguf_to_hub(gguf_dir: Path, repo_id: str, *, model_card: str, attribution: dict[str, Any],
                      private: bool = True, token: str | None = None) -> str:
    """Create (if needed) `repo_id` and upload the GGUF folder, model card, and attribution
    manifest. Returns the repo URL."""
    from huggingface_hub import HfApi

    api = HfApi(token=token)
    repo_url = api.create_repo(repo_id, private=private, exist_ok=True)
    api.upload_folder(repo_id=repo_id, folder_path=str(gguf_dir), allow_patterns=["*.gguf"])
    api.upload_file(path_or_fileobj=model_card.encode("utf-8"), path_in_repo="README.md", repo_id=repo_id)
    api.upload_file(path_or_fileobj=json.dumps(attribution, ensure_ascii=False, indent=2, sort_keys=True).encode(),
                     path_in_repo="ATTRIBUTION.json", repo_id=repo_id)
    return str(repo_url)
