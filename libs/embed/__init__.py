"""Local embedder: ONNX (Pi target) or a deterministic dev fallback."""

from libs.embed.embedder import EMBED_DIM, Embedder, HashEmbedder, load_embedder

__all__ = ["EMBED_DIM", "Embedder", "HashEmbedder", "load_embedder"]
