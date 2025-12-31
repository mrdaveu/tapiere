"""
Preference classifier for learning user taste.

Uses image embeddings + scikit-learn for lightweight binary classification.
"""

import pickle
import numpy as np
from pathlib import Path
from sklearn.linear_model import LogisticRegression
from sklearn.ensemble import RandomForestClassifier
from sklearn.preprocessing import StandardScaler
from sklearn.model_selection import cross_val_score
from sklearn.metrics import precision_score, recall_score, f1_score
from typing import Optional, Tuple

from database import (
    get_labeled_items_with_embeddings,
    get_items_with_embeddings,
    get_connection,
)

# Model storage path
MODELS_DIR = Path(__file__).parent / "models"
MODELS_DIR.mkdir(exist_ok=True)

MODEL_PATH = MODELS_DIR / "classifier.pkl"
SCALER_PATH = MODELS_DIR / "scaler.pkl"


class PreferenceClassifier:
    """
    Binary classifier for predicting user preferences from image embeddings.
    """

    def __init__(self, model_type: str = "logistic"):
        """
        Args:
            model_type: 'logistic' or 'random_forest'
        """
        self.model_type = model_type
        self.model = None
        self.scaler = StandardScaler()
        self.is_fitted = False
        self.metrics = {}

    def create_model(self):
        """Create a new model instance."""
        if self.model_type == "logistic":
            # Logistic regression with class balancing
            return LogisticRegression(
                class_weight="balanced",
                max_iter=1000,
                C=1.0,  # Regularization
                random_state=42
            )
        elif self.model_type == "random_forest":
            return RandomForestClassifier(
                n_estimators=100,
                class_weight="balanced",
                max_depth=10,
                random_state=42,
                n_jobs=-1
            )
        else:
            raise ValueError(f"Unknown model type: {self.model_type}")

    def train(
        self,
        embeddings: np.ndarray = None,
        labels: np.ndarray = None,
        evaluate: bool = True
    ) -> dict:
        """
        Train the classifier on labeled embeddings.

        Args:
            embeddings: Shape (n_samples, embedding_dim), or None to load from DB
            labels: Shape (n_samples,) with 0/1 values
            evaluate: Whether to run cross-validation

        Returns:
            dict with training metrics
        """
        # Load from database if not provided
        if embeddings is None or labels is None:
            embeddings, labels, _ = get_labeled_items_with_embeddings()

        if len(embeddings) == 0:
            print("No labeled data available for training")
            return {"error": "No training data"}

        n_positive = np.sum(labels == 1)
        n_negative = np.sum(labels == 0)
        print(f"Training on {len(labels)} samples ({n_positive} positive, {n_negative} negative)")

        if n_positive < 1:
            print("Warning: Need at least 1 positive example to train")
            return {"error": "No positive examples"}

        # Scale embeddings
        X = self.scaler.fit_transform(embeddings)
        y = labels

        # Create and train model
        self.model = self.create_model()

        # Cross-validation if we have enough samples
        if evaluate and len(labels) >= 10:
            n_splits = min(5, min(n_positive, n_negative))
            if n_splits >= 2:
                scores = cross_val_score(self.model, X, y, cv=n_splits, scoring="f1")
                self.metrics["cv_f1_mean"] = float(np.mean(scores))
                self.metrics["cv_f1_std"] = float(np.std(scores))
                print(f"Cross-validation F1: {self.metrics['cv_f1_mean']:.3f} (+/- {self.metrics['cv_f1_std']:.3f})")

        # Train on full data
        self.model.fit(X, y)
        self.is_fitted = True

        # Training set metrics
        y_pred = self.model.predict(X)
        self.metrics["train_precision"] = float(precision_score(y, y_pred, zero_division=0))
        self.metrics["train_recall"] = float(recall_score(y, y_pred, zero_division=0))
        self.metrics["train_f1"] = float(f1_score(y, y_pred, zero_division=0))
        self.metrics["n_samples"] = len(labels)
        self.metrics["n_positive"] = int(n_positive)
        self.metrics["n_negative"] = int(n_negative)

        print(f"Training complete - Precision: {self.metrics['train_precision']:.3f}, "
              f"Recall: {self.metrics['train_recall']:.3f}")

        return self.metrics

    def predict_proba(self, embeddings: np.ndarray) -> np.ndarray:
        """
        Predict probability of 'like' for each embedding.

        Args:
            embeddings: Shape (n_samples, embedding_dim)

        Returns:
            Probability scores (n_samples,) between 0 and 1
        """
        if not self.is_fitted:
            raise ValueError("Model not trained. Call train() first.")

        X = self.scaler.transform(embeddings)
        # Get probability of positive class
        proba = self.model.predict_proba(X)[:, 1]
        return proba

    def predict(self, embeddings: np.ndarray, threshold: float = 0.5) -> np.ndarray:
        """
        Predict binary labels.

        Args:
            embeddings: Shape (n_samples, embedding_dim)
            threshold: Probability threshold for positive prediction

        Returns:
            Binary predictions (n_samples,)
        """
        proba = self.predict_proba(embeddings)
        return (proba >= threshold).astype(int)

    def save(self, path: Path = MODEL_PATH):
        """Save model to disk."""
        if not self.is_fitted:
            raise ValueError("Cannot save unfitted model")

        data = {
            "model": self.model,
            "scaler": self.scaler,
            "model_type": self.model_type,
            "metrics": self.metrics
        }

        with open(path, "wb") as f:
            pickle.dump(data, f)
        print(f"Model saved to {path}")

    def load(self, path: Path = MODEL_PATH):
        """Load model from disk."""
        if not path.exists():
            raise ValueError(f"Model file not found: {path}")

        with open(path, "rb") as f:
            data = pickle.load(f)

        self.model = data["model"]
        self.scaler = data["scaler"]
        self.model_type = data["model_type"]
        self.metrics = data.get("metrics", {})
        self.is_fitted = True

        print(f"Model loaded from {path}")
        print(f"Trained on {self.metrics.get('n_samples', '?')} samples")


def score_items(
    item_ids: list = None,
    classifier: PreferenceClassifier = None,
    threshold: float = 0.3
) -> list:
    """
    Score items using the classifier.

    Args:
        item_ids: List of item IDs to score, or None for all items with embeddings
        classifier: Trained classifier, or None to load from disk
        threshold: Only return items above this score

    Returns:
        List of (item_id, score) tuples, sorted by score descending
    """
    # Load classifier if not provided
    if classifier is None:
        classifier = PreferenceClassifier()
        classifier.load()

    # Get items with embeddings
    conn = get_connection()
    cursor = conn.cursor()

    if item_ids is None:
        cursor.execute("""
            SELECT id, embedding FROM items
            WHERE embedding IS NOT NULL
        """)
    else:
        placeholders = ",".join("?" * len(item_ids))
        cursor.execute(f"""
            SELECT id, embedding FROM items
            WHERE id IN ({placeholders}) AND embedding IS NOT NULL
        """, item_ids)

    rows = cursor.fetchall()
    conn.close()

    if not rows:
        return []

    # Extract embeddings
    ids = [row["id"] for row in rows]
    embeddings = np.array([
        np.frombuffer(row["embedding"], dtype=np.float32)
        for row in rows
    ])

    # Score
    scores = classifier.predict_proba(embeddings)

    # Filter and sort
    results = [
        (item_id, float(score))
        for item_id, score in zip(ids, scores)
        if score >= threshold
    ]
    results.sort(key=lambda x: x[1], reverse=True)

    return results


def get_top_items(n: int = 100, threshold: float = 0.0) -> list:
    """
    Get top N items by predicted score.

    Returns list of dicts with item info and score.
    """
    # Load classifier
    try:
        classifier = PreferenceClassifier()
        classifier.load()
    except ValueError:
        # No trained model, return random items
        print("No trained classifier found, returning random items")
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT i.*, NULL as score FROM items i
            LEFT JOIN labels l ON i.id = l.item_id
            WHERE l.id IS NULL
            ORDER BY RANDOM()
            LIMIT ?
        """, (n,))
        items = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return items

    # Score all unlabeled items
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT i.id, i.embedding FROM items i
        LEFT JOIN labels l ON i.id = l.item_id
        WHERE l.id IS NULL AND i.embedding IS NOT NULL
    """)
    rows = cursor.fetchall()

    if not rows:
        conn.close()
        return []

    ids = [row["id"] for row in rows]
    embeddings = np.array([
        np.frombuffer(row["embedding"], dtype=np.float32)
        for row in rows
    ])

    # Score
    scores = classifier.predict_proba(embeddings)

    # Sort by score
    sorted_indices = np.argsort(scores)[::-1]

    # Get top N above threshold
    top_ids = []
    top_scores = []
    for idx in sorted_indices:
        if scores[idx] >= threshold:
            top_ids.append(ids[idx])
            top_scores.append(scores[idx])
        if len(top_ids) >= n:
            break

    if not top_ids:
        conn.close()
        return []

    # Fetch full item info
    placeholders = ",".join("?" * len(top_ids))
    cursor.execute(f"""
        SELECT * FROM items WHERE id IN ({placeholders})
    """, top_ids)
    items_by_id = {row["id"]: dict(row) for row in cursor.fetchall()}
    conn.close()

    # Add scores and maintain order
    results = []
    for item_id, score in zip(top_ids, top_scores):
        if item_id in items_by_id:
            item = items_by_id[item_id]
            item["score"] = score
            results.append(item)

    return results


def retrain():
    """Retrain classifier on all available labeled data."""
    classifier = PreferenceClassifier(model_type="logistic")
    metrics = classifier.train()

    if "error" not in metrics:
        classifier.save()

    return metrics


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        command = sys.argv[1]

        if command == "train":
            print("Training classifier...")
            metrics = retrain()
            print(f"\nMetrics: {metrics}")

        elif command == "score":
            print("Scoring items...")
            top_items = get_top_items(n=20)
            print(f"\nTop {len(top_items)} items:")
            for item in top_items:
                print(f"  [{item.get('score', 0):.3f}] {item.get('title', 'Unknown')[:50]} - ¥{item.get('price', '?')}")

    else:
        print("Usage:")
        print("  python classifier.py train   # Train on labeled data")
        print("  python classifier.py score   # Score unlabeled items")
