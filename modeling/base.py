"""Abstract Strategy interface for statistical football score models."""

from __future__ import annotations

from abc import ABC, abstractmethod

import numpy as np

from domain.entities import EncodedDataset, FittedModelState, ModelConfig


class BaseScoreModel(ABC):
    """Unified Strategy contract for fitting and predicting match goal rates."""

    @abstractmethod
    def fit(
        self,
        dataset: EncodedDataset,
        weights: np.ndarray | None = None,
        *,
        config: ModelConfig,
        prior_means: dict[str, float] | None = None,
    ) -> FittedModelState:
        """Fit model parameters on strictly pre-kickoff historical data."""

    @abstractmethod
    def predict_lambdas(
        self,
        state: FittedModelState,
        home_idx: int,
        away_idx: int,
        *,
        config: ModelConfig,
    ) -> tuple[float, float]:
        """Predict expected (lambda_home, lambda_away) for one fixture."""
