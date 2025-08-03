from abc import ABC, abstractmethod
import numpy as np
from sklearn.preprocessing import StandardScaler
from sklearn.cluster import KMeans
from sklearn.linear_model import LinearRegression
from sklearn.exceptions import ConvergenceWarning

from scores.PlotScore import PlotScore

import warnings
warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=ConvergenceWarning)


class BaseScore(ABC):
    def __init__(self, name, description, feaures, df, n_regimes, regime_labels ):
        self.name = name
        self.description = description
        self.df = df
        self.features = feaures
        self.n_regimes = n_regimes
        self.regime_labels = regime_labels

        self.regime_col = f"{self.name}_regime"
        self.regime_labels_col = f"{self.name}_regime_labels"  
        self.proxy_score_col = f"{self.name}_proxy_score"
        self.regression_target_col = f"{self.name}_regression_target"
        self.weights_per_regime = {}
        self.score = f"{self.name}_score"

    @abstractmethod
    def proxy_score(self):
        """_summary_: used to sort regimes in order to give them meaning
        """
        pass

    @abstractmethod
    def regression_target(self):
        """_summary_: used to find correlation or run regression on the features in order to find weights per regime
        """
        pass

    @abstractmethod
    def pipeline(self):
        """_summary_: sequence of calling
        regression_target()
        proxy_score()
        detect_regime_unsupervised()
        feature_regime_weights()
        score()
        """
        pass

    def detect_regime_unsupervised(self):
        """_summary_: detect regimes using unsupervised learning
        """
        kmeans = KMeans(n_clusters=self.n_regimes, random_state=42)
        self.df['cluster'] = kmeans.fit_predict(self.df[self.proxy_score_col].values.reshape(-1, 1))
        cluster_means = self.df.groupby('cluster')[self.proxy_score_col].mean()
        sorted_clusters = cluster_means.sort_values().index
        cluster_to_regime = {cluster: i for i, cluster in enumerate(sorted_clusters)}
        self.df[self.regime_col] = self.df['cluster'].map(cluster_to_regime)
        self.df[self.regime_labels_col] = self.df[self.regime_col].map(self.regime_labels)
        self.df.drop(columns=['cluster'], inplace=True)

    def feature_regime_weights(self, factor=1, alpha=0, l1_ratio=1):
        """_summary_: calculate weights per regime using regression
        """
        for regime in range(self.n_regimes):
            subset = self.df[self.df[self.regime_col] == regime]
            # check if this works
            if subset.empty:
                continue
            X_reg = StandardScaler().fit_transform(subset[self.features])
            y_reg = factor * subset[self.regression_target_col]

            # regularization helps avoid overfitting but too much can lead to underfitting
            # alpha is regularization(model will try harder to shrink coefficients),
            # alpha = 0 means linear regression
            # l1_ratio controls the mix between lasso(l1) and ridge(l2) kind of regularization
            # l1 --> tends to push coefficient to exactly zero, effectively doing feature selection
            # l2 --> shrinks the coefficient towards zero but doens eliminate them entirely
            # l1_ratio = 1 (feature selection), 0 (shrinkage), 0<l1_ratio<1 : both
            # use gridsearch or manual to find weights
            # fixed random_state for reproducability else set it to None
            # model = ElasticNet(alpha=alpha, l1_ratio=l1_ratio, random_state=42)
            model = LinearRegression()
            model.fit(X_reg, y_reg)
            self.weights_per_regime[regime] = model.coef_

    def calculate_score(self):
        """_summary_: calculates the index score using the features and per regime weights
        assumes regime_col has been filled ie. detect_regime_unsupervised() and feature_regime_weights() are executed
        """
        index_score = []
        scaled_features = StandardScaler().fit_transform(self.df[self.features])
        for idx, row in self.df.iterrows():
            regime = row[self.regime_col]
            current_scaled_features = scaled_features[self.df.index.get_loc(idx)]  # Get the index's location in scaled_features
            weights = self.weights_per_regime.get(regime, np.zeros(len(self.features)))
            regime_score = np.dot(current_scaled_features, weights)
            index_score.append(regime_score)
        self.df[self.score] = index_score

    def get_score(self):
        """_summary_: returns the score column
        """
        return self.df[self.score]

    def plot(self, y_list=None, category_column=None):
        if category_column is None:
            category_column = self.regime_col
        if y_list is None:
            y_list = [self.proxy_score_col]
        return PlotScore.multiple_lines(df=self.df, columns=y_list, category_column=category_column, regime_labels=self.regime_labels)
    
    def plot_violin(self, y=None, category_column=None):
        if category_column is None:
            category_column = self.regime_col
        if y is None:
            y = 'returns'
        return PlotScore.plot_violin(df=self.df, x_col=category_column, y_col=y)