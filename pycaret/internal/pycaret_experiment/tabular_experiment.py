import os
import gc
import logging
import random
import secrets
import traceback
import warnings
from typing import List, Tuple, Dict, Optional, Any, Union
from unittest.mock import patch
from joblib.memory import Memory
from packaging import version

import numpy as np  # type: ignore
import plotly.express as px  # type: ignore
import plotly.graph_objects as go  # type: ignore
import pycaret.internal.patches.sklearn
import pycaret.internal.patches.yellowbrick
import pycaret.internal.persistence
import pycaret.internal.preprocess
import scikitplot as skplt  # type: ignore
from pandas.io.formats.style import Styler
from pycaret.internal.logging import create_logger
from pycaret.internal.meta_estimators import get_estimator_from_meta_estimator
from pycaret.internal.pipeline import Pipeline as InternalPipeline
from pycaret.internal.pipeline import (
    estimator_pipeline,
    get_pipeline_estimator_label,
    get_pipeline_fit_kwargs,
    get_memory,
)
from pycaret.internal.plots.helper import MatplotlibDefaultDPI
from pycaret.internal.plots.yellowbrick import show_yellowbrick_plot
from pycaret.internal.pycaret_experiment.pycaret_experiment import _PyCaretExperiment
from pycaret.internal.pycaret_experiment.utils import MLUsecase
from pycaret.internal.utils import (
    check_features_exist,
    get_model_name,
    mlflow_remove_bad_chars,
)
from pycaret.internal.validation import *

from pycaret.internal.Display import Display

from sklearn.model_selection import BaseCrossValidator  # type: ignore


warnings.filterwarnings("ignore")
LOGGER = get_logger()


class _TabularExperiment(_PyCaretExperiment):
    def __init__(self) -> None:
        super().__init__()
        self.variable_keys = self.variable_keys.union(
            {
                "_ml_usecase",
                "_available_plots",
                "variable_keys",
                "USI",
                "html_param",
                "seed",
                "pipeline",
                "experiment__",
                "n_jobs_param",
                "_gpu_n_jobs_param",
                "master_model_container",
                "display_container",
                "exp_name_log",
                "exp_id",
                "logging_param",
                "log_plots_param",
                "data",
                "idx",
                "gpu_param",
                "_all_models",
                "_all_models_internal",
                "_all_metrics",
                "pipeline",
                "memory",
                "imputation_regressor",
                "imputation_classifier",
                "iterative_imputation_iters_param",
            }
        )
        return

    def _get_setup_display(self, **kwargs) -> Styler:
        return pd.DataFrame().style

    def _get_default_plots_to_log(self) -> List[str]:
        return []

    def _get_groups(
        self,
        groups,
        data: Optional[pd.DataFrame] = None,
        fold_groups=None,
    ):
        import pycaret.internal.utils

        data = data if data is not None else self.X_train
        fold_groups = fold_groups if fold_groups is not None else self.fold_groups_param
        return pycaret.internal.utils.get_groups(groups, data, fold_groups)

    def _get_cv_splitter(
        self, fold, ml_usecase: Optional[MLUsecase] = None
    ) -> BaseCrossValidator:
        """Returns the cross validator object used to perform cross validation"""
        if not ml_usecase:
            ml_usecase = self._ml_usecase

        import pycaret.internal.utils

        return pycaret.internal.utils.get_cv_splitter(
            fold,
            default=self.fold_generator,
            seed=self.seed,
            shuffle=self.fold_shuffle_param,
            int_default="stratifiedkfold"
            if ml_usecase == MLUsecase.CLASSIFICATION
            else "kfold",
        )

    def _is_unsupervised(self) -> bool:
        return False

    def _get_model_id(self, e, models=None) -> str:
        """
        Get model id.
        """
        if models is None:
            models = self._all_models_internal

        return pycaret.internal.utils.get_model_id(e, models)

    def _get_metric_by_name_or_id(self, name_or_id: str, metrics: Optional[Any] = None):
        """
        Gets a metric from get_metrics() by name or index.
        """
        if metrics is None:
            metrics = self._all_metrics
        metric = None
        try:
            metric = metrics[name_or_id]
            return metric
        except Exception:
            pass

        try:
            metric = next(
                v for k, v in metrics.items() if name_or_id in (v.display_name, v.name)
            )
            return metric
        except Exception:
            pass

        return metric

    def _get_model_name(self, e, deep: bool = True, models=None) -> str:
        """
        Get model name.
        """
        if models is None:
            models = self._all_models_internal

        return get_model_name(e, models, deep=deep)

    def _mlflow_log_model(
        self,
        model,
        model_results,
        score_dict: dict,
        source: str,
        runtime: float,
        model_fit_time: float,
        pipeline,
        log_holdout: bool = True,
        log_plots: bool = False,
        tune_cv_results=None,
        URI=None,
        experiment_custom_tags=None,
        display: Optional[Display] = None,
    ):
        self.logger.info("Creating MLFlow logs")

        # Creating Logs message monitor
        if display:
            display.update_monitor(1, "Creating Logs")
            display.display_monitor()

        # import mlflow
        import mlflow
        import mlflow.sklearn

        mlflow.set_experiment(self.exp_name_log)

        full_name = self._get_model_name(model)
        self.logger.info(f"Model: {full_name}")

        with mlflow.start_run(run_name=full_name, nested=True) as run:

            # Get active run to log as tag
            RunID = mlflow.active_run().info.run_id

            # Log model parameters
            pipeline_estimator_name = get_pipeline_estimator_label(model)
            if pipeline_estimator_name:
                params = model.named_steps[pipeline_estimator_name]
            else:
                params = model

            # get regressor from meta estimator
            params = get_estimator_from_meta_estimator(params)

            try:
                try:
                    params = params.get_all_params()
                except Exception:
                    params = params.get_params()
            except Exception:
                self.logger.warning("Couldn't get params for model. Exception:")
                self.logger.warning(traceback.format_exc())
                params = {}

            for i in list(params):
                v = params.get(i)
                if len(str(v)) > 250:
                    params.pop(i)

            params = {mlflow_remove_bad_chars(k): v for k, v in params.items()}
            self.logger.info(f"logged params: {params}")
            mlflow.log_params(params)

            # Log metrics
            def try_make_float(val):
                try:
                    return np.float64(val)
                except Exception:
                    return np.nan

            score_dict = {k: try_make_float(v) for k, v in score_dict.items()}
            self.logger.info(f"logged metrics: {score_dict}")
            mlflow.log_metrics(score_dict)

            # set tag of compare_models
            mlflow.set_tag("Source", source)

            # set custom tags if applicable
            if experiment_custom_tags:
                mlflow.set_tags(experiment_custom_tags)

            if not URI:
                import secrets

                URI = secrets.token_hex(nbytes=4)
            mlflow.set_tag("URI", URI)
            mlflow.set_tag("USI", self.USI)
            mlflow.set_tag("Run Time", runtime)
            mlflow.set_tag("Run ID", RunID)

            # Log training time in seconds
            mlflow.log_metric("TT", model_fit_time)

            # Log the CV results as model_results.html artifact
            if not self._is_unsupervised():
                try:
                    model_results.data.to_html(
                        "Results.html", col_space=65, justify="left"
                    )
                except Exception:
                    model_results.to_html("Results.html", col_space=65, justify="left")
                mlflow.log_artifact("Results.html")
                os.remove("Results.html")

                if log_holdout:
                    # Generate hold-out predictions and save as html
                    try:
                        holdout = self.predict_model(model, verbose=False)  # type: ignore
                        holdout_score = self.pull(pop=True)
                        del holdout
                        holdout_score.to_html(
                            "Holdout.html", col_space=65, justify="left"
                        )
                        mlflow.log_artifact("Holdout.html")
                        os.remove("Holdout.html")
                    except Exception:
                        self.logger.warning(
                            "Couldn't create holdout prediction for model, exception below:"
                        )
                        self.logger.warning(traceback.format_exc())

            # Log AUC and Confusion Matrix plot
            if log_plots:

                self.logger.info(
                    "SubProcess plot_model() called =================================="
                )

                def _log_plot(plot):
                    try:
                        plot_name = self.plot_model(
                            model, plot=plot, verbose=False, save=True, system=False
                        )
                        mlflow.log_artifact(plot_name)
                        os.remove(plot_name)
                    except Exception as e:
                        self.logger.warning(e)

                for plot in log_plots:
                    _log_plot(plot)

                self.logger.info(
                    "SubProcess plot_model() end =================================="
                )

            # Log hyperparameter tuning grid
            if tune_cv_results:
                d1 = tune_cv_results.get("params")
                dd = pd.DataFrame.from_dict(d1)
                dd["Score"] = tune_cv_results.get("mean_test_score")
                dd.to_html("Iterations.html", col_space=75, justify="left")
                mlflow.log_artifact("Iterations.html")
                os.remove("Iterations.html")

            # get default conda env
            from mlflow.sklearn import get_default_conda_env

            default_conda_env = get_default_conda_env()
            default_conda_env["name"] = f"{self.exp_name_log}-env"
            default_conda_env.get("dependencies").pop(-3)
            dependencies = default_conda_env.get("dependencies")[-1]
            from pycaret.utils import __version__

            dep = f"pycaret=={__version__}"
            dependencies["pip"] = [dep]

            # define model signature
            from mlflow.models.signature import infer_signature

            try:
                signature = infer_signature(self.data.drop([self.target_param], axis=1))
            except Exception:
                self.logger.warning("Couldn't infer MLFlow signature.")
                signature = None
            if not self._is_unsupervised():
                input_example = (
                    self.data.drop([self.target_param], axis=1).iloc[0].to_dict()
                )
            else:
                input_example = self.data.iloc[0].to_dict()

            # log model as sklearn flavor
            pipeline_temp = deepcopy(pipeline)
            pipeline_temp.steps.append(["trained_model", model])
            mlflow.sklearn.log_model(
                pipeline_temp,
                "model",
                conda_env=default_conda_env,
                signature=signature,
                input_example=input_example,
            )
            del pipeline_temp
        gc.collect()

    def _profile(self, profile, profile_kwargs):
        if profile:
            profile_kwargs = profile_kwargs or {}

            if self.verbose:
                print("Loading profile... Please Wait!")
            try:
                import pandas_profiling

                self.report = pandas_profiling.ProfileReport(
                    self.data, **profile_kwargs
                )
            except Exception as ex:
                print("Profiler Failed. No output to show, continue with modeling.")
                self.logger.error(
                    f"Data Failed with exception:\n {ex}\n"
                    "No output to show, continue with modeling."
                )

    def _initialize_setup(
        self,
        n_jobs: Optional[int] = -1,
        use_gpu: bool = False,
        html: bool = True,
        session_id: Optional[int] = None,
        system_log: Union[bool, logging.Logger] = True,
        log_experiment: bool = False,
        experiment_name: Optional[str] = None,
        memory: Union[bool, str, Memory] = True,
        verbose: bool = True,
    ):
        """
        This function initializes the environment in pycaret.
        setup() must called before executing any other function in pycaret. It
        takes only two mandatory parameters: data and name of the target column.

        """
        from pycaret.utils import __version__

        # Parameter attrs
        self.n_jobs_param = n_jobs
        self.gpu_param = use_gpu
        self.html_param = html
        self.logging_param = log_experiment
        self.memory = get_memory(memory)
        self.verbose = verbose

        # Global attrs
        self.USI = secrets.token_hex(nbytes=2)
        self.seed = random.randint(150, 9000) if session_id is None else session_id
        np.random.seed(self.seed)

        # Initialization =========================================== >>

        # Get local parameters to write to logger
        function_params_str = ", ".join(
            [f"{k}={v}" for k, v in locals().items() if k != "self"]
        )

        if experiment_name:
            if not isinstance(experiment_name, str):
                raise TypeError(
                    "The experiment_name parameter must be a non-empty str if not None."
                )
            self.exp_name_log = experiment_name

        self.logger = create_logger(system_log)
        self.logger.info(f"PyCaret {type(self).__name__}")
        self.logger.info(f"Logging name: {self.exp_name_log}")
        self.logger.info(f"ML Usecase: {self._ml_usecase}")
        self.logger.info(f"version {__version__}")
        self.logger.info("Initializing setup()")
        self.logger.info(f"self.USI: {self.USI}")

        self.logger.info(f"self.variable_keys: {self.variable_keys}")

        self._check_enviroment()

        # Set up GPU usage ========================================= >>

        if self.gpu_param != "force" and type(self.gpu_param) is not bool:
            raise TypeError(
                f"Invalid value for the use_gpu parameter, got {self.gpu_param}. "
                "Possible values are: 'force', True or False."
            )

        cuml_version = None
        if self.gpu_param:
            self.logger.info("Set up GPU usage.")

            try:
                from cuml import __version__

                cuml_version = __version__
                self.logger.info(f"cuml=={cuml_version}")

                cuml_version = cuml_version.split(".")
                cuml_version = (int(cuml_version[0]), int(cuml_version[1]))
            except Exception:
                self.logger.warning("cuML not found")

            if cuml_version is None or not version.parse(cuml_version) >= version.parse(
                "0.15"
            ):
                message = f"cuML is outdated or not found. Required version is >=0.15, got {__version__}"
                if use_gpu == "force":
                    raise ImportError(message)
                else:
                    self.logger.warning(message)

    @staticmethod
    def plot_model_check_display_format_(display_format: Optional[str]):
        """Checks if the display format is in the allowed list"""
        plot_formats = [None, "streamlit"]

        if display_format not in plot_formats:
            raise ValueError("display_format can only be None or 'streamlit'.")

    def plot_model(
        self,
        estimator,
        plot: str = "auc",
        scale: float = 1,  # added in pycaret==2.1.0
        save: Union[str, bool] = False,
        fold: Optional[Union[int, Any]] = None,
        fit_kwargs: Optional[dict] = None,
        groups: Optional[Union[str, Any]] = None,
        feature_name: Optional[str] = None,
        label: bool = False,
        use_train_data: bool = False,
        verbose: bool = True,
        system: bool = True,
        display: Optional[Display] = None,  # added in pycaret==2.2.0
        display_format: Optional[str] = None,
    ) -> str:

        """
        This function takes a trained model object and returns a plot based on the
        test / hold-out set. The process may require the model to be re-trained in
        certain cases. See list of plots supported below.

        Model must be created using create_model() or tune_model().

        Example
        -------
        >>> from pycaret.datasets import get_data
        >>> juice = get_data('juice')
        >>> experiment_name = setup(data = juice,  target = 'Purchase')
        >>> lr = create_model('lr')
        >>> plot_model(lr)

        This will return an AUC plot of a trained Logistic Regression model.

        Parameters
        ----------
        estimator : object, default = none
            A trained model object should be passed as an estimator.

        plot : str, default = auc
            Enter abbreviation of type of plot. The current list of plots supported are (Plot - Name):

            * 'residuals_interactive' - Interactive Residual plots
            * 'auc' - Area Under the Curve
            * 'threshold' - Discrimination Threshold
            * 'pr' - Precision Recall Curve
            * 'confusion_matrix' - Confusion Matrix
            * 'error' - Class Prediction Error
            * 'class_report' - Classification Report
            * 'boundary' - Decision Boundary
            * 'rfe' - Recursive Feature Selection
            * 'learning' - Learning Curve
            * 'manifold' - Manifold Learning
            * 'calibration' - Calibration Curve
            * 'vc' - Validation Curve
            * 'dimension' - Dimension Learning
            * 'feature' - Feature Importance
            * 'feature_all' - Feature Importance (All)
            * 'parameter' - Model Hyperparameter
            * 'lift' - Lift Curve
            * 'gain' - Gain Chart

        scale: float, default = 1
            The resolution scale of the figure.

        save: string or bool, default = False
            When set to True, Plot is saved as a 'png' file in current working directory.
            When a path destination is given, Plot is saved as a 'png' file the given path to the directory of choice.

        fold: integer or scikit-learn compatible CV generator, default = None
            Controls cross-validation used in certain plots. If None, will use the CV generator
            defined in setup(). If integer, will use KFold CV with that many folds.
            When cross_validation is False, this parameter is ignored.

        fit_kwargs: dict, default = {} (empty dict)
            Dictionary of arguments passed to the fit method of the model.

        groups: str or array-like, with shape (n_samples,), default = None
            Optional Group labels for the samples used while splitting the dataset into train/test set.
            If string is passed, will use the data column with that name as the groups.
            Only used if a group based cross-validation generator is used (eg. GroupKFold).
            If None, will use the value set in fold_groups parameter in setup().

        verbose: bool, default = True
            Progress bar not shown when verbose set to False.

        system: bool, default = True
            Must remain True all times. Only to be changed by internal functions.

        display_format: str, default = None
            To display plots in Streamlit (https://www.streamlit.io/), set this to 'streamlit'.
            Currently, not all plots are supported.

        Returns
        -------
        Visual_Plot
            Prints the visual plot.
        str:
            If save parameter is True, will return the name of the saved file.

        Warnings
        --------
        -  'svm' and 'ridge' doesn't support the predict_proba method. As such, AUC and
            calibration plots are not available for these estimators.

        -   When the 'max_features' parameter of a trained model object is not equal to
            the number of samples in training set, the 'rfe' plot is not available.

        -   'calibration', 'threshold', 'manifold' and 'rfe' plots are not available for
            multiclass problems.


        """

        function_params_str = ", ".join([f"{k}={v}" for k, v in locals().items()])

        self.logger.info("Initializing plot_model()")
        self.logger.info(f"plot_model({function_params_str})")

        self.logger.info("Checking exceptions")

        if not fit_kwargs:
            fit_kwargs = {}

        if not hasattr(estimator, "fit"):
            raise ValueError(
                f"Estimator {estimator} does not have the required fit() method."
            )

        if plot not in self._available_plots:
            raise ValueError(
                "Plot Not Available. Please see docstring for list of available Plots."
            )

        # checking display_format parameter
        self.plot_model_check_display_format_(display_format=display_format)

        # Import required libraries ----
        if display_format == "streamlit":
            try:
                import streamlit as st
            except ImportError:
                raise ImportError(
                    "It appears that streamlit is not installed. Do: pip install hpbandster ConfigSpace"
                )

        # multiclass plot exceptions:
        multiclass_not_available = ["calibration", "threshold", "manifold", "rfe"]
        if self._is_multiclass():
            if plot in multiclass_not_available:
                raise ValueError(
                    "Plot Not Available for multiclass problems. Please see docstring for list of available Plots."
                )

        # exception for CatBoost
        # if "CatBoostClassifier" in str(type(estimator)):
        #    raise ValueError(
        #    "CatBoost estimator is not compatible with plot_model function, try using Catboost with interpret_model instead."
        # )

        # checking for auc plot
        if not hasattr(estimator, "predict_proba") and plot == "auc":
            raise TypeError(
                "AUC plot not available for estimators with no predict_proba attribute."
            )

        # checking for auc plot
        if not hasattr(estimator, "predict_proba") and plot == "auc":
            raise TypeError(
                "AUC plot not available for estimators with no predict_proba attribute."
            )

        # checking for calibration plot
        if not hasattr(estimator, "predict_proba") and plot == "calibration":
            raise TypeError(
                "Calibration plot not available for estimators with no predict_proba attribute."
            )

        def is_tree(e):
            from sklearn.ensemble._forest import BaseForest
            from sklearn.tree import BaseDecisionTree

            if "final_estimator" in e.get_params():
                e = e.final_estimator
            if "base_estimator" in e.get_params():
                e = e.base_estimator
            if isinstance(e, BaseForest) or isinstance(e, BaseDecisionTree):
                return True

        # checking for calibration plot
        if plot == "tree" and not is_tree(estimator):
            raise TypeError(
                "Decision Tree plot is only available for scikit-learn Decision Trees and Forests, Ensemble models using those or Stacked models using those as meta (final) estimators."
            )

        # checking for feature plot
        if not (
            hasattr(estimator, "coef_") or hasattr(estimator, "feature_importances_")
        ) and (plot == "feature" or plot == "feature_all" or plot == "rfe"):
            raise TypeError(
                "Feature Importance and RFE plots not available for estimators that doesnt support coef_ or feature_importances_ attribute."
            )

        # checking fold parameter
        if fold is not None and not (
            type(fold) is int or is_sklearn_cv_generator(fold)
        ):
            raise TypeError(
                "fold parameter must be either None, an integer or a scikit-learn compatible CV generator object."
            )

        if type(label) is not bool:
            raise TypeError("Label parameter only accepts True or False.")

        if type(use_train_data) is not bool:
            raise TypeError("use_train_data parameter only accepts True or False.")

        if feature_name is not None and type(feature_name) is not str:
            raise TypeError(
                "feature parameter must be string containing column name of dataset."
            )

        """

        ERROR HANDLING ENDS HERE

        """

        cv = self._get_cv_splitter(fold)

        groups = self._get_groups(groups)

        if not display:
            progress_args = {"max": 5}
            display = Display(
                verbose=verbose, html_param=self.html_param, progress_args=progress_args
            )
            display.display_progress()

        self.logger.info("Preloading libraries")
        # pre-load libraries
        import matplotlib.pyplot as plt

        np.random.seed(self.seed)

        display.move_progress()

        # defining estimator as model locally
        # deepcopy instead of clone so we have a fitted estimator
        if isinstance(estimator, InternalPipeline):
            estimator = estimator.steps[-1][1]
        estimator = deepcopy(estimator)
        model = estimator

        display.move_progress()

        # plots used for logging (controlled through plots_log_param)
        # AUC, #Confusion Matrix and #Feature Importance

        self.logger.info("Copying training dataset")

        self.logger.info(f"Plot type: {plot}")
        plot_name = self._available_plots[plot]
        display.move_progress()

        # yellowbrick workaround start
        import yellowbrick.utils.helpers
        import yellowbrick.utils.types

        # yellowbrick workaround end

        model_name = self._get_model_name(model)
        plot_filename = f"{plot_name}.png"
        with patch(
            "yellowbrick.utils.types.is_estimator",
            pycaret.internal.patches.yellowbrick.is_estimator,
        ):
            with patch(
                "yellowbrick.utils.helpers.is_estimator",
                pycaret.internal.patches.yellowbrick.is_estimator,
            ):
                _base_dpi = 100

                def residuals_interactive():
                    from pycaret.internal.plots.residual_plots import (
                        InteractiveResidualsPlot,
                    )

                    resplots = InteractiveResidualsPlot(
                        x=self.X_train_transformed,
                        y=self.y_train_transformed,
                        x_test=self.X_test_transformed,
                        y_test=self.y_test_transformed,
                        model=estimator,
                        display=display,
                    )

                    display.clear_output()
                    if system:
                        resplots.show()

                    plot_filename = f"{plot_name}.html"

                    if save:
                        if not isinstance(save, bool):
                            plot_filename = os.path.join(save, plot_filename)
                        else:
                            plot_filename = plot
                        self.logger.info(f"Saving '{plot_filename}'")
                        resplots.write_html(plot_filename)

                    self.logger.info("Visual Rendered Successfully")
                    return plot_filename

                def cluster():
                    self.logger.info(
                        "SubProcess assign_model() called =================================="
                    )
                    b = self.assign_model(  # type: ignore
                        estimator, verbose=False, transformation=True
                    ).reset_index(drop=True)
                    self.logger.info(
                        "SubProcess assign_model() end =================================="
                    )
                    cluster = b["Cluster"].values
                    b.drop("Cluster", axis=1, inplace=True)
                    b = pd.get_dummies(b)  # casting categorical variable

                    from sklearn.decomposition import PCA

                    pca = PCA(n_components=2, random_state=self.seed)
                    self.logger.info("Fitting PCA()")
                    pca_ = pca.fit_transform(b)
                    pca_ = pd.DataFrame(pca_)
                    pca_ = pca_.rename(columns={0: "PCA1", 1: "PCA2"})
                    pca_["Cluster"] = cluster

                    if feature_name is not None:
                        pca_["Feature"] = self.data[feature_name]
                    else:
                        pca_["Feature"] = self.data[self.data.columns[0]]

                    if label:
                        pca_["Label"] = pca_["Feature"]

                    """
                    sorting
                    """

                    self.logger.info("Sorting dataframe")

                    clus_num = [int(i.split()[1]) for i in pca_["Cluster"]]

                    pca_["cnum"] = clus_num
                    pca_.sort_values(by="cnum", inplace=True)

                    """
                    sorting ends
                    """

                    display.clear_output()

                    self.logger.info("Rendering Visual")

                    if label:
                        fig = px.scatter(
                            pca_,
                            x="PCA1",
                            y="PCA2",
                            text="Label",
                            color="Cluster",
                            opacity=0.5,
                        )
                    else:
                        fig = px.scatter(
                            pca_,
                            x="PCA1",
                            y="PCA2",
                            hover_data=["Feature"],
                            color="Cluster",
                            opacity=0.5,
                        )

                    fig.update_traces(textposition="top center")
                    fig.update_layout(plot_bgcolor="rgb(240,240,240)")

                    fig.update_layout(
                        height=600 * scale, title_text="2D Cluster PCA Plot"
                    )

                    plot_filename = f"{plot_name}.html"

                    if save:
                        if not isinstance(save, bool):
                            plot_filename = os.path.join(save, plot_filename)
                        else:
                            plot_filename = plot
                        self.logger.info(f"Saving '{plot_filename}'")
                        fig.write_html(plot_filename)

                    elif system:
                        if display_format == "streamlit":
                            st.write(fig)
                        else:
                            fig.show()

                    self.logger.info("Visual Rendered Successfully")
                    return plot_filename

                def umap():
                    self.logger.info(
                        "SubProcess assign_model() called =================================="
                    )
                    b = self.assign_model(  # type: ignore
                        model, verbose=False, transformation=True, score=False
                    ).reset_index(drop=True)
                    self.logger.info(
                        "SubProcess assign_model() end =================================="
                    )

                    label = pd.DataFrame(b["Anomaly"])
                    b.dropna(axis=0, inplace=True)  # droping rows with NA's
                    b.drop(["Anomaly"], axis=1, inplace=True)

                    import umap

                    reducer = umap.UMAP()
                    self.logger.info("Fitting UMAP()")
                    embedding = reducer.fit_transform(b)
                    X = pd.DataFrame(embedding)

                    import plotly.express as px

                    df = X
                    df["Anomaly"] = label

                    if feature_name is not None:
                        df["Feature"] = self.data[feature_name]
                    else:
                        df["Feature"] = self.data[self.data.columns[0]]

                    display.clear_output()

                    self.logger.info("Rendering Visual")

                    fig = px.scatter(
                        df,
                        x=0,
                        y=1,
                        color="Anomaly",
                        title="uMAP Plot for Outliers",
                        hover_data=["Feature"],
                        opacity=0.7,
                        width=900 * scale,
                        height=800 * scale,
                    )
                    plot_filename = f"{plot_name}.html"

                    if save:
                        if not isinstance(save, bool):
                            plot_filename = os.path.join(save, plot_filename)
                        else:
                            plot_filename = plot
                        self.logger.info(f"Saving '{plot_filename}'")
                        fig.write_html(f"{plot_filename}")

                    elif system:
                        if display_format == "streamlit":
                            st.write(fig)
                        else:
                            fig.show()

                    self.logger.info("Visual Rendered Successfully")
                    return plot_filename

                def tsne():
                    if self._ml_usecase == MLUsecase.CLUSTERING:
                        return _tsne_clustering()
                    else:
                        return _tsne_anomaly()

                def _tsne_anomaly():
                    self.logger.info(
                        "SubProcess assign_model() called =================================="
                    )
                    b = self.assign_model(  # type: ignore
                        model, verbose=False, transformation=True, score=False
                    ).reset_index(drop=True)
                    self.logger.info(
                        "SubProcess assign_model() end =================================="
                    )
                    cluster = b["Anomaly"].values
                    b.dropna(axis=0, inplace=True)  # droping rows with NA's
                    b.drop("Anomaly", axis=1, inplace=True)

                    self.logger.info("Getting dummies to cast categorical variables")

                    from sklearn.manifold import TSNE

                    self.logger.info("Fitting TSNE()")
                    X_embedded = TSNE(n_components=3).fit_transform(b)

                    X = pd.DataFrame(X_embedded)
                    X["Anomaly"] = cluster
                    if feature_name is not None:
                        X["Feature"] = self.data[feature_name]
                    else:
                        X["Feature"] = self.data[self.data.columns[0]]

                    df = X

                    display.clear_output()

                    self.logger.info("Rendering Visual")

                    if label:
                        fig = px.scatter_3d(
                            df,
                            x=0,
                            y=1,
                            z=2,
                            text="Feature",
                            color="Anomaly",
                            title="3d TSNE Plot for Outliers",
                            opacity=0.7,
                            width=900 * scale,
                            height=800 * scale,
                        )
                    else:
                        fig = px.scatter_3d(
                            df,
                            x=0,
                            y=1,
                            z=2,
                            hover_data=["Feature"],
                            color="Anomaly",
                            title="3d TSNE Plot for Outliers",
                            opacity=0.7,
                            width=900 * scale,
                            height=800 * scale,
                        )

                    plot_filename = f"{plot_name}.html"

                    if save:
                        if not isinstance(save, bool):
                            plot_filename = os.path.join(save, plot_filename)
                        else:
                            plot_filename = plot
                        self.logger.info(f"Saving '{plot_filename}'")
                        fig.write_html(f"{plot_filename}")

                    elif system:
                        if display_format == "streamlit":
                            st.write(fig)
                        else:
                            fig.show()

                    self.logger.info("Visual Rendered Successfully")
                    return plot_filename

                def _tsne_clustering():
                    self.logger.info(
                        "SubProcess assign_model() called =================================="
                    )
                    b = self.assign_model(  # type: ignore
                        estimator,
                        verbose=False,
                        score=False,
                        transformation=True,
                    ).reset_index(drop=True)
                    self.logger.info(
                        "SubProcess assign_model() end =================================="
                    )

                    cluster = b["Cluster"].values
                    b.drop("Cluster", axis=1, inplace=True)

                    from sklearn.manifold import TSNE

                    self.logger.info("Fitting TSNE()")
                    X_embedded = TSNE(
                        n_components=3, random_state=self.seed
                    ).fit_transform(b)
                    X_embedded = pd.DataFrame(X_embedded)
                    X_embedded["Cluster"] = cluster

                    if feature_name is not None:
                        X_embedded["Feature"] = self.data[feature_name]
                    else:
                        X_embedded["Feature"] = self.data[self.data.columns[0]]

                    if label:
                        X_embedded["Label"] = X_embedded["Feature"]

                    """
                    sorting
                    """
                    self.logger.info("Sorting dataframe")

                    clus_num = [int(i.split()[1]) for i in X_embedded["Cluster"]]

                    X_embedded["cnum"] = clus_num
                    X_embedded.sort_values(by="cnum", inplace=True)

                    """
                    sorting ends
                    """

                    df = X_embedded

                    display.clear_output()

                    self.logger.info("Rendering Visual")

                    if label:

                        fig = px.scatter_3d(
                            df,
                            x=0,
                            y=1,
                            z=2,
                            color="Cluster",
                            title="3d TSNE Plot for Clusters",
                            text="Label",
                            opacity=0.7,
                            width=900 * scale,
                            height=800 * scale,
                        )

                    else:
                        fig = px.scatter_3d(
                            df,
                            x=0,
                            y=1,
                            z=2,
                            color="Cluster",
                            title="3d TSNE Plot for Clusters",
                            hover_data=["Feature"],
                            opacity=0.7,
                            width=900 * scale,
                            height=800 * scale,
                        )

                    plot_filename = f"{plot_name}.html"

                    if save:
                        if not isinstance(save, bool):
                            plot_filename = os.path.join(save, plot_filename)
                        else:
                            plot_filename = plot
                        self.logger.info(f"Saving '{plot_filename}'")
                        fig.write_html(f"{plot_filename}")

                    elif system:
                        if display_format == "streamlit":
                            st.write(fig)
                        else:
                            fig.show()

                    self.logger.info("Visual Rendered Successfully")
                    return plot_filename

                def distribution():
                    self.logger.info(
                        "SubProcess assign_model() called =================================="
                    )
                    d = self.assign_model(  # type: ignore
                        estimator, verbose=False
                    ).reset_index(drop=True)
                    self.logger.info(
                        "SubProcess assign_model() end =================================="
                    )

                    """
                    sorting
                    """
                    self.logger.info("Sorting dataframe")

                    clus_num = []
                    for i in d.Cluster:
                        a = int(i.split()[1])
                        clus_num.append(a)

                    d["cnum"] = clus_num
                    d.sort_values(by="cnum", inplace=True)
                    d.reset_index(inplace=True, drop=True)

                    clus_label = []
                    for i in d.cnum:
                        a = "Cluster " + str(i)
                        clus_label.append(a)

                    d.drop(["Cluster", "cnum"], inplace=True, axis=1)
                    d["Cluster"] = clus_label

                    """
                    sorting ends
                    """

                    if feature_name is None:
                        x_col = "Cluster"
                    else:
                        x_col = feature_name

                    display.clear_output()

                    self.logger.info("Rendering Visual")

                    fig = px.histogram(
                        d,
                        x=x_col,
                        color="Cluster",
                        marginal="box",
                        opacity=0.7,
                        hover_data=d.columns,
                    )

                    fig.update_layout(
                        height=600 * scale,
                    )

                    plot_filename = f"{plot_name}.html"

                    if save:
                        if not isinstance(save, bool):
                            plot_filename = os.path.join(save, plot_filename)
                        else:
                            plot_filename = plot
                        self.logger.info(f"Saving '{plot_filename}'")
                        fig.write_html(f"{plot_filename}")

                    elif system:
                        if display_format == "streamlit":
                            st.write(fig)
                        else:
                            fig.show()

                    self.logger.info("Visual Rendered Successfully")
                    return plot_filename

                def elbow():
                    try:
                        from yellowbrick.cluster import KElbowVisualizer

                        visualizer = KElbowVisualizer(estimator, timings=False)
                        show_yellowbrick_plot(
                            visualizer=visualizer,
                            X_train=self.X_train_transformed,
                            y_train=None,
                            X_test=None,
                            y_test=None,
                            name=plot_name,
                            handle_test="",
                            scale=scale,
                            save=save,
                            fit_kwargs=fit_kwargs,
                            groups=groups,
                            display=display,
                            display_format=display_format,
                        )

                    except:
                        self.logger.error("Elbow plot failed. Exception:")
                        self.logger.error(traceback.format_exc())
                        raise TypeError("Plot Type not supported for this model.")

                def silhouette():
                    from yellowbrick.cluster import SilhouetteVisualizer

                    try:
                        visualizer = SilhouetteVisualizer(
                            estimator, colors="yellowbrick"
                        )
                        show_yellowbrick_plot(
                            visualizer=visualizer,
                            X_train=self.X_train_transformed,
                            y_train=None,
                            X_test=None,
                            y_test=None,
                            name=plot_name,
                            handle_test="",
                            scale=scale,
                            save=save,
                            fit_kwargs=fit_kwargs,
                            groups=groups,
                            display=display,
                            display_format=display_format,
                        )
                    except:
                        self.logger.error("Silhouette plot failed. Exception:")
                        self.logger.error(traceback.format_exc())
                        raise TypeError("Plot Type not supported for this model.")

                def distance():
                    from yellowbrick.cluster import InterclusterDistance

                    try:
                        visualizer = InterclusterDistance(estimator)
                        show_yellowbrick_plot(
                            visualizer=visualizer,
                            X_train=self.X_train_transformed,
                            y_train=None,
                            X_test=None,
                            y_test=None,
                            name=plot_name,
                            handle_test="",
                            scale=scale,
                            save=save,
                            fit_kwargs=fit_kwargs,
                            groups=groups,
                            display=display,
                            display_format=display_format,
                        )
                    except:
                        self.logger.error("Distance plot failed. Exception:")
                        self.logger.error(traceback.format_exc())
                        raise TypeError("Plot Type not supported for this model.")

                def residuals():

                    from yellowbrick.regressor import ResidualsPlot

                    visualizer = ResidualsPlot(estimator)
                    show_yellowbrick_plot(
                        visualizer=visualizer,
                        X_train=self.X_train_transformed,
                        y_train=self.y_train_transformed,
                        X_test=self.X_test_transformed,
                        y_test=self.y_test_transformed,
                        name=plot_name,
                        scale=scale,
                        save=save,
                        fit_kwargs=fit_kwargs,
                        groups=groups,
                        display=display,
                        display_format=display_format,
                    )

                def auc():

                    from yellowbrick.classifier import ROCAUC

                    visualizer = ROCAUC(estimator)
                    show_yellowbrick_plot(
                        visualizer=visualizer,
                        X_train=self.X_train_transformed,
                        y_train=self.y_train_transformed,
                        X_test=self.X_test_transformed,
                        y_test=self.y_test_transformed,
                        name=plot_name,
                        scale=scale,
                        save=save,
                        fit_kwargs=fit_kwargs,
                        groups=groups,
                        display=display,
                        display_format=display_format,
                    )

                def threshold():

                    from yellowbrick.classifier import DiscriminationThreshold

                    visualizer = DiscriminationThreshold(
                        estimator, random_state=self.seed
                    )
                    show_yellowbrick_plot(
                        visualizer=visualizer,
                        X_train=self.X_train_transformed,
                        y_train=self.y_train_transformed,
                        X_test=self.X_test_transformed,
                        y_test=self.y_test_transformed,
                        name=plot_name,
                        scale=scale,
                        save=save,
                        fit_kwargs=fit_kwargs,
                        groups=groups,
                        display=display,
                        display_format=display_format,
                    )

                def pr():

                    from yellowbrick.classifier import PrecisionRecallCurve

                    visualizer = PrecisionRecallCurve(estimator, random_state=self.seed)
                    show_yellowbrick_plot(
                        visualizer=visualizer,
                        X_train=self.X_train_transformed,
                        y_train=self.y_train_transformed,
                        X_test=self.X_test_transformed,
                        y_test=self.y_test_transformed,
                        name=plot_name,
                        scale=scale,
                        save=save,
                        fit_kwargs=fit_kwargs,
                        groups=groups,
                        display=display,
                        display_format=display_format,
                    )

                def confusion_matrix():

                    from yellowbrick.classifier import ConfusionMatrix

                    visualizer = ConfusionMatrix(
                        estimator,
                        random_state=self.seed,
                        fontsize=15,
                        cmap="Greens",
                    )
                    show_yellowbrick_plot(
                        visualizer=visualizer,
                        X_train=self.X_train_transformed,
                        y_train=self.y_train_transformed,
                        X_test=self.X_test_transformed,
                        y_test=self.y_test_transformed,
                        name=plot_name,
                        scale=scale,
                        save=save,
                        fit_kwargs=fit_kwargs,
                        groups=groups,
                        display=display,
                        display_format=display_format,
                    )

                def error():

                    if self._ml_usecase == MLUsecase.CLASSIFICATION:
                        from yellowbrick.classifier import ClassPredictionError

                        visualizer = ClassPredictionError(
                            estimator, random_state=self.seed
                        )

                    elif self._ml_usecase == MLUsecase.REGRESSION:
                        from yellowbrick.regressor import PredictionError

                        visualizer = PredictionError(estimator, random_state=self.seed)

                    show_yellowbrick_plot(
                        visualizer=visualizer,  # type: ignore
                        X_train=self.X_train_transformed,
                        y_train=self.y_train_transformed,
                        X_test=self.X_test_transformed,
                        y_test=self.y_test_transformed,
                        name=plot_name,
                        scale=scale,
                        save=save,
                        fit_kwargs=fit_kwargs,
                        groups=groups,
                        display=display,
                        display_format=display_format,
                    )

                def cooks():

                    from yellowbrick.regressor import CooksDistance

                    visualizer = CooksDistance()
                    show_yellowbrick_plot(
                        visualizer=visualizer,
                        X_train=self.X,
                        y_train=self.y,
                        X_test=self.X_test_transformed,
                        y_test=self.y_test_transformed,
                        name=plot_name,
                        scale=scale,
                        save=save,
                        fit_kwargs=fit_kwargs,
                        handle_test="",
                        groups=groups,
                        display=display,
                        display_format=display_format,
                    )

                def class_report():

                    from yellowbrick.classifier import ClassificationReport

                    visualizer = ClassificationReport(
                        estimator,
                        random_state=self.seed,
                        support=True,
                    )
                    show_yellowbrick_plot(
                        visualizer=visualizer,
                        X_train=self.X_train_transformed,
                        y_train=self.y_train_transformed,
                        X_test=self.X_test_transformed,
                        y_test=self.y_test_transformed,
                        name=plot_name,
                        scale=scale,
                        save=save,
                        fit_kwargs=fit_kwargs,
                        groups=groups,
                        display=display,
                        display_format=display_format,
                    )

                def boundary():

                    from sklearn.decomposition import PCA
                    from sklearn.preprocessing import StandardScaler
                    from yellowbrick.contrib.classifier import DecisionViz

                    data_X_transformed = self.X_train_transformed.select_dtypes(
                        include="number"
                    )
                    test_X_transformed = self.X_test_transformed.select_dtypes(
                        include="number"
                    )
                    self.logger.info("Fitting StandardScaler()")
                    data_X_transformed = StandardScaler().fit_transform(
                        data_X_transformed
                    )
                    test_X_transformed = StandardScaler().fit_transform(
                        test_X_transformed
                    )
                    pca = PCA(n_components=2, random_state=self.seed)
                    self.logger.info("Fitting PCA()")
                    data_X_transformed = pca.fit_transform(data_X_transformed)
                    test_X_transformed = pca.fit_transform(test_X_transformed)

                    viz_ = DecisionViz(estimator)
                    show_yellowbrick_plot(
                        visualizer=viz_,
                        X_train=data_X_transformed,
                        y_train=np.array(self.y_train_transformed),
                        X_test=test_X_transformed,
                        y_test=np.array(self.y_test_transformed),
                        name=plot_name,
                        scale=scale,
                        handle_test="draw",
                        save=save,
                        fit_kwargs=fit_kwargs,
                        groups=groups,
                        display=display,
                        features=["Feature One", "Feature Two"],
                        classes=["A", "B"],
                        display_format=display_format,
                    )

                def rfe():

                    from yellowbrick.model_selection import RFECV

                    visualizer = RFECV(estimator, cv=cv)
                    show_yellowbrick_plot(
                        visualizer=visualizer,
                        X_train=self.X_train_transformed,
                        y_train=self.y_train_transformed,
                        X_test=self.X_test_transformed,
                        y_test=self.y_test_transformed,
                        handle_test="",
                        name=plot_name,
                        scale=scale,
                        save=save,
                        fit_kwargs=fit_kwargs,
                        groups=groups,
                        display=display,
                        display_format=display_format,
                    )

                def learning():

                    from yellowbrick.model_selection import LearningCurve

                    sizes = np.linspace(0.3, 1.0, 10)
                    visualizer = LearningCurve(
                        estimator,
                        cv=cv,
                        train_sizes=sizes,
                        n_jobs=self._gpu_n_jobs_param,
                        random_state=self.seed,
                    )
                    show_yellowbrick_plot(
                        visualizer=visualizer,
                        X_train=self.X_train_transformed,
                        y_train=self.y_train_transformed,
                        X_test=self.X_test_transformed,
                        y_test=self.y_test_transformed,
                        handle_test="",
                        name=plot_name,
                        scale=scale,
                        save=save,
                        fit_kwargs=fit_kwargs,
                        groups=groups,
                        display=display,
                        display_format=display_format,
                    )

                def lift():

                    display.move_progress()
                    self.logger.info("Generating predictions / predict_proba on X_test")
                    y_test__ = self.y_test_transformed
                    predict_proba__ = estimator.predict_proba(self.X_test_transformed)
                    display.move_progress()
                    display.move_progress()
                    display.clear_output()
                    with MatplotlibDefaultDPI(base_dpi=_base_dpi, scale_to_set=scale):
                        fig = skplt.metrics.plot_lift_curve(
                            y_test__, predict_proba__, figsize=(10, 6)
                        )
                        if save:
                            plot_filename = f"{plot_name}.png"
                            if not isinstance(save, bool):
                                plot_filename = os.path.join(save, plot_filename)
                            self.logger.info(f"Saving '{plot_filename}'")
                            plt.savefig(plot_filename, bbox_inches="tight")
                        elif system:
                            plt.show()
                        plt.close()

                    self.logger.info("Visual Rendered Successfully")

                def gain():

                    display.move_progress()
                    self.logger.info("Generating predictions / predict_proba on X_test")
                    y_test__ = self.y_test_transformed
                    predict_proba__ = estimator.predict_proba(self.X_test_transformed)
                    display.move_progress()
                    display.move_progress()
                    display.clear_output()
                    with MatplotlibDefaultDPI(base_dpi=_base_dpi, scale_to_set=scale):
                        fig = skplt.metrics.plot_cumulative_gain(
                            y_test__, predict_proba__, figsize=(10, 6)
                        )
                        if save:
                            plot_filename = f"{plot_name}.png"
                            if not isinstance(save, bool):
                                plot_filename = os.path.join(save, plot_filename)
                            self.logger.info(f"Saving '{plot_filename}'")
                            plt.savefig(plot_filename, bbox_inches="tight")
                        elif system:
                            plt.show()
                        plt.close()

                    self.logger.info("Visual Rendered Successfully")

                def manifold():

                    from yellowbrick.features import Manifold

                    data_X_transformed = self.X_train_transformed.select_dtypes(
                        include="number"
                    )
                    visualizer = Manifold(manifold="tsne", random_state=self.seed)
                    show_yellowbrick_plot(
                        visualizer=visualizer,
                        X_train=data_X_transformed,
                        y_train=self.y_train_transformed,
                        X_test=self.X_test_transformed,
                        y_test=self.y_test_transformed,
                        handle_train="fit_transform",
                        handle_test="",
                        name=plot_name,
                        scale=scale,
                        save=save,
                        fit_kwargs=fit_kwargs,
                        groups=groups,
                        display=display,
                        display_format=display_format,
                    )

                def tree():

                    from sklearn.base import is_classifier
                    from sklearn.model_selection import check_cv
                    from sklearn.tree import plot_tree

                    is_stacked_model = False
                    is_ensemble_of_forests = False

                    if "final_estimator" in estimator.get_params():
                        estimator = estimator.final_estimator
                        is_stacked_model = True

                    if (
                        "base_estimator" in estimator.get_params()
                        and "n_estimators" in estimator.base_estimator.get_params()
                    ):
                        n_estimators = (
                            estimator.get_params()["n_estimators"]
                            * estimator.base_estimator.get_params()["n_estimators"]
                        )
                        is_ensemble_of_forests = True
                    elif "n_estimators" in estimator.get_params():
                        n_estimators = estimator.get_params()["n_estimators"]
                    else:
                        n_estimators = 1
                    if n_estimators > 10:
                        rows = (n_estimators // 10) + 1
                        cols = 10
                    else:
                        rows = 1
                        cols = n_estimators
                    figsize = (cols * 20, rows * 16)
                    fig, axes = plt.subplots(
                        nrows=rows,
                        ncols=cols,
                        figsize=figsize,
                        dpi=_base_dpi * scale,
                        squeeze=False,
                    )
                    axes = list(axes.flatten())

                    fig.suptitle("Decision Trees")

                    display.move_progress()
                    self.logger.info("Plotting decision trees")
                    trees = []
                    feature_names = list(self.X_train_transformed.columns)
                    if self._ml_usecase == MLUsecase.CLASSIFICATION:
                        class_names = {
                            v: k
                            for k, v in self.pipeline.named_steps[
                                "dtypes"
                            ].replacement.items()
                        }
                    else:
                        class_names = None
                    fitted_estimator = estimator.steps[-1][1]
                    if is_stacked_model:
                        stacked_feature_names = []
                        if self._ml_usecase == MLUsecase.CLASSIFICATION:
                            classes = list(self.y_train_transformed.unique())
                            if len(classes) == 2:
                                classes.pop()
                            for c in classes:
                                stacked_feature_names.extend(
                                    [
                                        f"{k}_{class_names[c]}"
                                        for k, v in fitted_estimator.estimators
                                    ]
                                )
                        else:
                            stacked_feature_names.extend(
                                [f"{k}" for k, v in fitted_estimator.estimators]
                            )
                        if not fitted_estimator.passthrough:
                            feature_names = stacked_feature_names
                        else:
                            feature_names = stacked_feature_names + feature_names
                        fitted_estimator = fitted_estimator.final_estimator_
                    if is_ensemble_of_forests:
                        for estimator in fitted_estimator.estimators_:
                            trees.extend(estimator.estimators_)
                    else:
                        try:
                            trees = fitted_estimator.estimators_
                        except:
                            trees = [fitted_estimator]
                    if self._ml_usecase == MLUsecase.CLASSIFICATION:
                        class_names = list(class_names.values())
                    for i, tree in enumerate(trees):
                        self.logger.info(f"Plotting tree {i}")
                        plot_tree(
                            tree,
                            feature_names=feature_names,
                            class_names=class_names,
                            filled=True,
                            rounded=True,
                            precision=4,
                            ax=axes[i],
                        )
                        axes[i].set_title(f"Tree {i}")
                    for i in range(len(trees), len(axes)):
                        axes[i].set_visible(False)
                    display.move_progress()

                    display.move_progress()
                    display.clear_output()
                    if save:
                        plot_filename = f"{plot_name}.png"
                        if not isinstance(save, bool):
                            plot_filename = os.path.join(save, plot_filename)
                        self.logger.info(f"Saving '{plot_filename}'")
                        plt.savefig(plot_filename, bbox_inches="tight")
                    elif system:
                        plt.show()
                    plt.close()

                    self.logger.info("Visual Rendered Successfully")

                def calibration():

                    from sklearn.calibration import calibration_curve

                    plt.figure(figsize=(7, 6), dpi=_base_dpi * scale)
                    ax1 = plt.subplot2grid((3, 1), (0, 0), rowspan=2)

                    ax1.plot([0, 1], [0, 1], "k:", label="Perfectly calibrated")
                    display.move_progress()
                    self.logger.info("Scoring test/hold-out set")
                    prob_pos = estimator.predict_proba(self.X_test_transformed)[:, 1]
                    prob_pos = (prob_pos - prob_pos.min()) / (
                        prob_pos.max() - prob_pos.min()
                    )
                    (
                        fraction_of_positives,
                        mean_predicted_value,
                    ) = calibration_curve(self.y_test_transformed, prob_pos, n_bins=10)
                    display.move_progress()
                    ax1.plot(
                        mean_predicted_value,
                        fraction_of_positives,
                        "s-",
                        label=f"{model_name}",
                    )

                    ax1.set_ylabel("Fraction of positives")
                    ax1.set_ylim([0, 1])
                    ax1.set_xlim([0, 1])
                    ax1.legend(loc="lower right")
                    ax1.set_title("Calibration plots (reliability curve)")
                    ax1.set_facecolor("white")
                    ax1.grid(b=True, color="grey", linewidth=0.5, linestyle="-")
                    plt.tight_layout()
                    display.move_progress()
                    display.clear_output()
                    if save:
                        plot_filename = f"{plot_name}.png"
                        if not isinstance(save, bool):
                            plot_filename = os.path.join(save, plot_filename)
                        self.logger.info(f"Saving '{plot_filename}'")
                        plt.savefig(plot_filename, bbox_inches="tight")
                    elif system:
                        plt.show()
                    plt.close()

                    self.logger.info("Visual Rendered Successfully")

                def vc():

                    self.logger.info("Determining param_name")

                    try:
                        try:
                            # catboost special case
                            model_params = estimator.get_all_params()
                        except:
                            model_params = estimator.get_params()
                    except:
                        display.clear_output()
                        self.logger.error("VC plot failed. Exception:")
                        self.logger.error(traceback.format_exc())
                        raise TypeError(
                            "Plot not supported for this estimator. Try different estimator."
                        )

                    param_name = ""
                    param_range = None

                    if self._ml_usecase == MLUsecase.CLASSIFICATION:

                        # Catboost
                        if "depth" in model_params:
                            param_name = "depth"
                            param_range = np.arange(1, 8 if self.gpu_param else 11)

                        # SGD Classifier
                        elif "l1_ratio" in model_params:
                            param_name = "l1_ratio"
                            param_range = np.arange(0, 1, 0.01)

                        # tree based models
                        elif "max_depth" in model_params:
                            param_name = "max_depth"
                            param_range = np.arange(1, 11)

                        # knn
                        elif "n_neighbors" in model_params:
                            param_name = "n_neighbors"
                            param_range = np.arange(1, 11)

                        # MLP / Ridge
                        elif "alpha" in model_params:
                            param_name = "alpha"
                            param_range = np.arange(0, 1, 0.1)

                        # Logistic Regression
                        elif "C" in model_params:
                            param_name = "C"
                            param_range = np.arange(1, 11)

                        # Bagging / Boosting
                        elif "n_estimators" in model_params:
                            param_name = "n_estimators"
                            param_range = np.arange(1, 1000, 10)

                        # Naive Bayes
                        elif "var_smoothing" in model_params:
                            param_name = "var_smoothing"
                            param_range = np.arange(0.1, 1, 0.01)

                        # QDA
                        elif "reg_param" in model_params:
                            param_name = "reg_param"
                            param_range = np.arange(0, 1, 0.1)

                        # GPC
                        elif "max_iter_predict" in model_params:
                            param_name = "max_iter_predict"
                            param_range = np.arange(100, 1000, 100)

                        else:
                            display.clear_output()
                            raise TypeError(
                                "Plot not supported for this estimator. Try different estimator."
                            )

                    elif self._ml_usecase == MLUsecase.REGRESSION:

                        # Catboost
                        if "depth" in model_params:
                            param_name = "depth"
                            param_range = np.arange(1, 8 if self.gpu_param else 11)

                        # lasso/ridge/en/llar/huber/kr/mlp/br/ard
                        elif "alpha" in model_params:
                            param_name = "alpha"
                            param_range = np.arange(0, 1, 0.1)

                        elif "alpha_1" in model_params:
                            param_name = "alpha_1"
                            param_range = np.arange(0, 1, 0.1)

                        # par/svm
                        elif "C" in model_params:
                            param_name = "C"
                            param_range = np.arange(1, 11)

                        # tree based models (dt/rf/et)
                        elif "max_depth" in model_params:
                            param_name = "max_depth"
                            param_range = np.arange(1, 11)

                        # knn
                        elif "n_neighbors" in model_params:
                            param_name = "n_neighbors"
                            param_range = np.arange(1, 11)

                        # Bagging / Boosting (ada/gbr)
                        elif "n_estimators" in model_params:
                            param_name = "n_estimators"
                            param_range = np.arange(1, 1000, 10)

                        # Bagging / Boosting (ada/gbr)
                        elif "n_nonzero_coefs" in model_params:
                            param_name = "n_nonzero_coefs"
                            if len(self.X_train_transformed.columns) >= 10:
                                param_max = 11
                            else:
                                param_max = len(self.X_train_transformed.columns) + 1
                            param_range = np.arange(1, param_max, 1)

                        elif "eps" in model_params:
                            param_name = "eps"
                            param_range = np.arange(0, 1, 0.1)

                        elif "max_subpopulation" in model_params:
                            param_name = "max_subpopulation"
                            param_range = np.arange(1000, 100000, 2000)

                        elif "min_samples" in model_params:
                            param_name = "min_samples"
                            param_range = np.arange(0.01, 1, 0.1)

                        else:
                            display.clear_output()
                            raise TypeError(
                                "Plot not supported for this estimator. Try different estimator."
                            )

                    self.logger.info(f"param_name: {param_name}")

                    display.move_progress()

                    from yellowbrick.model_selection import ValidationCurve

                    viz = ValidationCurve(
                        estimator,
                        param_name=param_name,
                        param_range=param_range,
                        cv=cv,
                        random_state=self.seed,
                        n_jobs=self._gpu_n_jobs_param,
                    )
                    show_yellowbrick_plot(
                        visualizer=viz,
                        X_train=self.X_train_transformed,
                        y_train=self.y_train_transformed,
                        X_test=self.X_test_transformed,
                        y_test=self.y_test_transformed,
                        handle_train="fit",
                        handle_test="",
                        name=plot_name,
                        scale=scale,
                        save=save,
                        fit_kwargs=fit_kwargs,
                        groups=groups,
                        display=display,
                        display_format=display_format,
                    )

                def dimension():

                    from sklearn.decomposition import PCA
                    from sklearn.preprocessing import StandardScaler
                    from yellowbrick.features import RadViz

                    data_X_transformed = self.X_train_transformed.select_dtypes(
                        include="number"
                    )
                    self.logger.info("Fitting StandardScaler()")
                    data_X_transformed = StandardScaler().fit_transform(
                        data_X_transformed
                    )

                    features = min(
                        round(len(self.X_train_transformed.columns) * 0.3, 0), 5
                    )
                    features = int(features)

                    pca = PCA(n_components=features, random_state=self.seed)
                    self.logger.info("Fitting PCA()")
                    data_X_transformed = pca.fit_transform(data_X_transformed)
                    display.move_progress()
                    classes = self.y_train_transformed.unique().tolist()
                    visualizer = RadViz(classes=classes, alpha=0.25)

                    show_yellowbrick_plot(
                        visualizer=visualizer,
                        X_train=data_X_transformed,
                        y_train=np.array(self.y_train_transformed),
                        X_test=self.X_test_transformed,
                        y_test=self.y_test_transformed,
                        handle_train="fit_transform",
                        handle_test="",
                        name=plot_name,
                        scale=scale,
                        save=save,
                        fit_kwargs=fit_kwargs,
                        groups=groups,
                        display=display,
                        display_format=display_format,
                    )

                def feature():
                    _feature(10)

                def feature_all():
                    _feature(len(self.X_train_transformed.columns))

                def _feature(n: int):
                    variables = None
                    temp_model = estimator
                    if hasattr(estimator, "steps"):
                        temp_model = estimator.steps[-1][1]
                    if hasattr(temp_model, "coef_"):
                        try:
                            coef = temp_model.coef_.flatten()
                            if len(coef) > len(self.X_train_transformed.columns):
                                coef = coef[: len(self.X_train_transformed.columns)]
                            variables = abs(coef)
                        except:
                            pass
                    if variables is None:
                        self.logger.warning(
                            "No coef_ found. Trying feature_importances_"
                        )
                        variables = abs(temp_model.feature_importances_)
                    coef_df = pd.DataFrame(
                        {
                            "Variable": self.X_train_transformed.columns,
                            "Value": variables,
                        }
                    )
                    sorted_df = (
                        coef_df.sort_values(by="Value", ascending=False)
                        .head(n)
                        .sort_values(by="Value")
                    )
                    my_range = range(1, len(sorted_df.index) + 1)
                    display.move_progress()
                    plt.figure(figsize=(8, 5 * (n // 10)), dpi=_base_dpi * scale)
                    plt.hlines(
                        y=my_range,
                        xmin=0,
                        xmax=sorted_df["Value"],
                        color="skyblue",
                    )
                    plt.plot(sorted_df["Value"], my_range, "o")
                    display.move_progress()
                    plt.yticks(my_range, sorted_df["Variable"])
                    plt.title("Feature Importance Plot")
                    plt.xlabel("Variable Importance")
                    plt.ylabel("Features")
                    display.move_progress()
                    display.clear_output()
                    if save:
                        plot_filename = f"{plot_name}.png"
                        if not isinstance(save, bool):
                            plot_filename = os.path.join(save, plot_filename)
                        self.logger.info(f"Saving '{plot_filename}'")
                        plt.savefig(plot_filename, bbox_inches="tight")
                    elif system:
                        plt.show()
                    plt.close()

                    self.logger.info("Visual Rendered Successfully")

                def parameter():

                    try:
                        params = estimator.get_all_params()
                    except:
                        params = estimator.get_params(deep=False)

                    param_df = pd.DataFrame.from_dict(
                        {str(k): str(v) for k, v in params.items()},
                        orient="index",
                        columns=["Parameters"],
                    )
                    display.display(param_df, clear=True)
                    self.logger.info("Visual Rendered Successfully")

                def ks():

                    display.move_progress()
                    self.logger.info("Generating predictions / predict_proba on X_test")
                    predict_proba__ = estimator.predict_proba(self.X_train_transformed)
                    display.move_progress()
                    display.move_progress()
                    display.clear_output()
                    with MatplotlibDefaultDPI(base_dpi=_base_dpi, scale_to_set=scale):
                        fig = skplt.metrics.plot_ks_statistic(
                            self.y_train_transformed, predict_proba__, figsize=(10, 6)
                        )
                        if save:
                            plot_filename = f"{plot_name}.png"
                            if not isinstance(save, bool):
                                plot_filename = os.path.join(save, plot_filename)
                            self.logger.info(f"Saving '{plot_filename}'")
                            plt.savefig(plot_filename, bbox_inches="tight")
                        elif system:
                            plt.show()
                        plt.close()

                    self.logger.info("Visual Rendered Successfully")

                # execute the plot method
                ret = locals()[plot]()
                if ret:
                    plot_filename = ret

                try:
                    plt.close()
                except:
                    pass

        gc.collect()

        self.logger.info(
            "plot_model() successfully completed......................................"
        )

        if save:
            return plot_filename

    def evaluate_model(
        self,
        estimator,
        fold: Optional[Union[int, Any]] = None,
        fit_kwargs: Optional[dict] = None,
        feature_name: Optional[str] = None,
        groups: Optional[Union[str, Any]] = None,
        use_train_data: bool = False,
    ):

        """
        This function displays a user interface for all of the available plots for
        a given estimator. It internally uses the plot_model() function.

        Example
        -------
        >>> from pycaret.datasets import get_data
        >>> juice = get_data('juice')
        >>> experiment_name = setup(data = juice,  target = 'Purchase')
        >>> lr = create_model('lr')
        >>> evaluate_model(lr)

        This will display the User Interface for all of the plots for a given
        estimator.

        Parameters
        ----------
        estimator : object, default = none
            A trained model object should be passed as an estimator.

        fold: integer or scikit-learn compatible CV generator, default = None
            Controls cross-validation. If None, will use the CV generator defined in setup().
            If integer, will use KFold CV with that many folds.
            When cross_validation is False, this parameter is ignored.

        fit_kwargs: dict, default = {} (empty dict)
            Dictionary of arguments passed to the fit method of the model.

        groups: str or array-like, with shape (n_samples,), default = None
            Optional Group labels for the samples used while splitting the dataset into train/test set.
            If string is passed, will use the data column with that name as the groups.
            Only used if a group based cross-validation generator is used (eg. GroupKFold).
            If None, will use the value set in fold_groups parameter in setup().

        Returns
        -------
        User_Interface
            Displays the user interface for plotting.

        """

        function_params_str = ", ".join([f"{k}={v}" for k, v in locals().items()])

        self.logger.info("Initializing evaluate_model()")
        self.logger.info(f"evaluate_model({function_params_str})")

        from ipywidgets import widgets
        from ipywidgets.widgets import fixed, interact

        if not fit_kwargs:
            fit_kwargs = {}

        a = widgets.ToggleButtons(
            options=[(v, k) for k, v in self._available_plots.items()],
            description="Plot Type:",
            disabled=False,
            button_style="",  # 'success', 'info', 'warning', 'danger' or ''
            icons=[""],
        )

        fold = self._get_cv_splitter(fold)

        groups = self._get_groups(groups)

        interact(
            self.plot_model,
            estimator=fixed(estimator),
            plot=a,
            save=fixed(False),
            verbose=fixed(True),
            scale=fixed(1),
            fold=fixed(fold),
            fit_kwargs=fixed(fit_kwargs),
            feature_name=fixed(feature_name),
            label=fixed(False),
            groups=fixed(groups),
            use_train_data=fixed(use_train_data),
            system=fixed(True),
            display=fixed(None),
            display_format=fixed(None),
        )

    def predict_model(self, *args, **kwargs) -> pd.DataFrame:
        return pd.DataFrame()

    def finalize_model(self) -> None:
        return

    def automl(
        self, optimize: str = "Accuracy", use_holdout: bool = False, turbo: bool = True
    ) -> Any:

        """
        This function returns the best model out of all models created in
        current active environment based on metric defined in optimize parameter.

        Parameters
        ----------
        optimize : str, default = 'Accuracy'
            Other values you can pass in optimize parameter are 'AUC', 'Recall', 'Precision',
            'F1', 'Kappa', and 'MCC'.

        use_holdout: bool, default = False
            When set to True, metrics are evaluated on holdout set instead of CV.

        turbo: bool, default = True
            When set to True and use_holdout is False, only models created with default fold
            parameter will be considered. If set to False, models created with a non-default
            fold parameter will be scored again using default fold settings, so that they can be
            compared.
        """

        function_params_str = ", ".join([f"{k}={v}" for k, v in locals().items()])

        self.logger.info("Initializing automl()")
        self.logger.info(f"automl({function_params_str})")

        # checking optimize parameter
        optimize = self._get_metric_by_name_or_id(optimize)
        if optimize is None:
            raise ValueError(
                f"Optimize method not supported. See docstring for list of available parameters."
            )

        # checking optimize parameter for multiclass
        if self._is_multiclass():
            if not optimize.is_multiclass:
                raise TypeError(
                    f"Optimization metric not supported for multiclass problems. See docstring for list of other optimization parameters."
                )

        compare_dimension = optimize.display_name
        greater_is_better = optimize.greater_is_better
        optimize = optimize.scorer

        best_model = None
        best_score = None

        def compare_score(new, best):
            if not best:
                return True
            if greater_is_better:
                return new > best
            else:
                return new < best

        if use_holdout:
            self.logger.info("Model Selection Basis : Holdout set")
            for i in self.master_model_container:
                self.logger.info(f"Checking model {i}")
                model = i["model"]
                try:
                    pred_holdout = self.predict_model(model, verbose=False)  # type: ignore
                except:
                    self.logger.warning(
                        f"Model {model} is not fitted, running create_model"
                    )
                    model, _ = self.create_model(  # type: ignore
                        estimator=model,
                        system=False,
                        verbose=False,
                        cross_validation=False,
                        predict=False,
                        groups=self.fold_groups_param,
                    )
                    self.pull(pop=True)
                    pred_holdout = self.predict_model(model, verbose=False)  # type: ignore

                p = self.pull(pop=True)
                p = p[compare_dimension][0]
                if compare_score(p, best_score):
                    best_model = model
                    best_score = p

        else:
            self.logger.info("Model Selection Basis : CV Results on Training set")
            for i in range(len(self.master_model_container)):
                model = self.master_model_container[i]
                scores = None
                if model["cv"] is not self.fold_generator:
                    if turbo or self._is_unsupervised():
                        continue
                    self.create_model(  # type: ignore
                        estimator=model["model"],
                        system=False,
                        verbose=False,
                        cross_validation=True,
                        predict=False,
                        groups=self.fold_groups_param,
                    )
                    scores = self.pull(pop=True)
                    self.master_model_container.pop()
                self.logger.info(f"Checking model {i}")
                if scores is None:
                    scores = model["scores"]
                r = scores[compare_dimension][-2:][0]
                if compare_score(r, best_score):
                    best_model = model["model"]
                    best_score = r

        automl_model, _ = self.create_model(  # type: ignore
            estimator=best_model,
            system=False,
            verbose=False,
            cross_validation=False,
            predict=False,
            groups=self.fold_groups_param,
        )

        gc.collect()

        self.logger.info(str(automl_model))
        self.logger.info(
            "automl() successfully completed......................................"
        )

        return automl_model

    def _get_models(self, raise_errors: bool = True) -> Tuple[dict, dict]:
        return ({}, {})

    def _get_metrics(self, raise_errors: bool = True) -> dict:
        return {}

    def models(
        self,
        type: Optional[str] = None,
        internal: bool = False,
        raise_errors: bool = True,
    ) -> pd.DataFrame:

        """
        Returns table of models available in model library.

        Example
        -------
        >>> _all_models = models()

        This will return pandas dataframe with all available
        models and their metadata.

        Parameters
        ----------
        type : str, default = None
            - linear : filters and only return linear models
            - tree : filters and only return tree based models
            - ensemble : filters and only return ensemble models

        internal: bool, default = False
            If True, will return extra columns and rows used internally.

        raise_errors: bool, default = True
            If False, will suppress all exceptions, ignoring models
            that couldn't be created.

        Returns
        -------
        pandas.DataFrame

        """

        self.logger.info(f"gpu_param set to {self.gpu_param}")

        _, model_containers = self._get_models(raise_errors)

        rows = [
            v.get_dict(internal)
            for k, v in model_containers.items()
            if (internal or not v.is_special)
        ]

        df = pd.DataFrame(rows)
        df.set_index("ID", inplace=True, drop=True)

        return df

    def deploy_model(
        self,
        model,
        model_name: str,
        authentication: dict,
        platform: str = "aws",  # added gcp and azure support in pycaret==2.1
    ):

        """
        (In Preview)

        This function deploys the transformation pipeline and trained model object for
        production use. The platform of deployment can be defined under the platform
        parameter along with the applicable authentication tokens which are passed as a
        dictionary to the authentication param.

        Example
        -------
        >>> from pycaret.datasets import get_data
        >>> juice = get_data('juice')
        >>> experiment_name = setup(data = juice,  target = 'Purchase')
        >>> lr = create_model('lr')
        >>> deploy_model(model = lr, model_name = 'deploy_lr', platform = 'aws', authentication = {'bucket' : 'pycaret-test'})

        This will deploy the model on an AWS S3 account under bucket 'pycaret-test'

        Notes
        -----
        For AWS users:
        Before deploying a model to an AWS S3 ('aws'), environment variables must be
        configured using the command line interface. To configure AWS env. variables,
        type aws configure in your python command line. The following information is
        required which can be generated using the Identity and Access Management (IAM)
        portal of your amazon console account:

        - AWS Access Key ID
        - AWS Secret Key Access
        - Default Region Name (can be seen under Global settings on your AWS console)
        - Default output format (must be left blank)

        For GCP users:
        --------------
        Before deploying a model to Google Cloud Platform (GCP), project must be created
        either using command line or GCP console. Once project is created, you must create
        a service account and download the service account key as a JSON file, which is
        then used to set environment variable.

        https://cloud.google.com/docs/authentication/production

        - Google Cloud Project
        - Service Account Authetication

        For Azure users:
        ---------------
        Before deploying a model to Microsoft's Azure (Azure), environment variables
        for connection string must be set. In order to get connection string, user has
        to create account of Azure. Once it is done, create a Storage account. In the settings
        section of storage account, user can get the connection string.

        Read below link for more details.
        https://docs.microsoft.com/en-us/azure/storage/blobs/storage-quickstart-blobs-python?toc=%2Fpython%2Fazure%2FTOC.json

        - Azure Storage Account

        Parameters
        ----------
        model : object
            A trained model object should be passed as an estimator.

        model_name : str
            Name of model to be passed as a str.

        authentication : dict
            Dictionary of applicable authentication tokens.

            When platform = 'aws':
            {'bucket' : 'Name of Bucket on S3'}

            When platform = 'gcp':
            {'project': 'gcp_pycaret', 'bucket' : 'pycaret-test'}

            When platform = 'azure':
            {'container': 'pycaret-test'}

        platform: str, default = 'aws'
            Name of platform for deployment. Current available options are: 'aws', 'gcp' and 'azure'

        Returns
        -------
        Success_Message

        Warnings
        --------
        - This function uses file storage services to deploy the model on cloud platform.
        As such, this is efficient for batch-use. Where the production objective is to
        obtain prediction at an instance level, this may not be the efficient choice as
        it transmits the binary pickle file between your local python environment and
        the platform.

        """
        return pycaret.internal.persistence.deploy_model(
            model, model_name, authentication, platform, self.pipeline
        )

    def save_model(
        self,
        model,
        model_name: str,
        model_only: bool = False,
        verbose: bool = True,
        **kwargs,
    ) -> None:
        """
        This function saves the transformation pipeline and trained model object
        into the current active directory as a pickle file for later use.

        Example
        -------
        >>> from pycaret.datasets import get_data
        >>> juice = get_data('juice')
        >>> experiment_name = setup(data = juice,  target = 'Purchase')
        >>> lr = create_model('lr')
        >>> save_model(lr, 'lr_model_23122019')

        This will save the transformation pipeline and model as a binary pickle
        file in the current active directory.

        Parameters
        ----------
        model : object, default = none
            A trained model object should be passed as an estimator.

        model_name : str, default = none
            Name of pickle file to be passed as a string.

        model_only : bool, default = False
            When set to True, only trained model object is saved and all the
            transformations are ignored.

        verbose: bool, default = True
            Success message is not printed when verbose is set to False.

        Returns
        -------
        Success_Message

        """
        return pycaret.internal.persistence.save_model(
            model,
            model_name,
            None if model_only else self.pipeline,
            verbose,
            **kwargs,
        )

    def load_model(
        self,
        model_name,
        platform: Optional[str] = None,
        authentication: Optional[Dict[str, str]] = None,
        verbose: bool = True,
    ):

        """
        This function loads a previously saved transformation pipeline and model
        from the current active directory into the current python environment.
        Load object must be a pickle file.

        Example
        -------
        >>> saved_lr = load_model('lr_model_23122019')

        This will load the previously saved model in saved_lr variable. The file
        must be in the current directory.

        Parameters
        ----------
        model_name : str, default = none
            Name of pickle file to be passed as a string.

        platform: str, default = None
            Name of platform, if loading model from cloud. Current available options are:
            'aws', 'gcp' and 'azure'.

        authentication : dict
            dictionary of applicable authentication tokens.

            When platform = 'aws':
            {'bucket' : 'Name of Bucket on S3'}

            When platform = 'gcp':
            {'project': 'gcp_pycaret', 'bucket' : 'pycaret-test'}

            When platform = 'azure':
            {'container': 'pycaret-test'}

        verbose: bool, default = True
            Success message is not printed when verbose is set to False.

        Returns
        -------
        Model Object

        """

        return pycaret.internal.persistence.load_model(
            model_name, platform, authentication, verbose
        )
