"""Core library for the Trading Signal Dashboard.

Everything in this package is plain Python (pandas / numpy / scikit-learn) with no
Streamlit dependency, so it can be unit-tested and reused outside the UI.
"""

__all__ = ["config", "data", "indicators", "models", "tracker", "market"]
