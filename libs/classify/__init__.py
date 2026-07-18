"""Relevance classifier (PLAN §6.3 step 2): text-only newsworthiness score in [0,1]."""

from libs.classify.classifier import Classifier, HeuristicClassifier, load_classifier
from libs.classify.featurize import DIM, featurize

__all__ = ["DIM", "Classifier", "HeuristicClassifier", "featurize", "load_classifier"]
