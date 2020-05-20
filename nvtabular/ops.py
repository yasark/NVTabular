#
# Copyright (c) 2020, NVIDIA CORPORATION.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

import os

import cudf
import numpy as np
from cudf._lib.nvtx import annotate

from nvtabular.encoder import DLLabelEncoder
from nvtabular.groupby import GroupByMomentsCal

CONT = "continuous"
CAT = "categorical"
ALL = "all"


class Operator:
    """
    Base class for all operator classes.
    """

    def __init__(self, columns=None):
        self.columns = columns

    @property
    def _id(self):
        return str(self.__class__.__name__)

    def describe(self):
        raise NotImplementedError("All operators must have a desription.")

    def get_columns(self, cols_ctx, cols_grp, target_cols):
        # providing any operator with direct list of columns overwrites cols dict
        # burden on user to ensure columns exist in dataset (as discussed)
        if self.columns:
            return self.columns
        tar_cols = []
        for tar in target_cols:
            if tar in cols_ctx[cols_grp].keys():
                tar_cols = tar_cols + cols_ctx[cols_grp][tar]
        return tar_cols

    def export_op(self):
        export = {}
        export[str(self._id)] = self.__dict__
        return export


class TransformOperator(Operator):
    """
    Base class for transformer operator classes.
    """

    default_in = None
    default_out = None

    def __init__(self, columns=None, preprocessing=True, replace=True):
        super().__init__(columns=columns)
        self.preprocessing = preprocessing
        self.replace = replace

    def get_default_in(self):
        if self.default_in is None:
            raise NotImplementedError(
                "default_in columns have not been specified for this operator"
            )
        return self.default_in

    def get_default_out(self):
        if self.default_out is None:
            raise NotImplementedError(
                "default_out columns have not been specified for this operator"
            )
        return self.default_out

    def update_columns_ctx(self, columns_ctx, input_cols, new_cols, origin_targets, pro=False):
        """
        columns_ctx: columns context, belonging to the container workflow object
        input_cols: input columns; columns actioned on origin columns context key
        new_cols: new columns; new columns generated by operator to be added to columns context
        ----
        This function generalizes the action of updating the columns context dictionary
        of the container workflow object, after an operator has created new columns via a
        new transformation of a subset or entire dataset.
        """

        new_key = self._id

        if not pro:
            input_cols = self.default_out
        columns_ctx[input_cols][new_key] = []
        if self.replace and self.preprocessing:
            # not making new columns instead using old ones
            # must reference original target with new operator for chaining
            columns_ctx[input_cols][new_key] = origin_targets
            return
        columns_ctx[input_cols][new_key] = list(new_cols)
        if not self.preprocessing and self._id not in columns_ctx["final"]["ctx"][input_cols]:
            columns_ctx["final"]["ctx"][input_cols].append(self._id)

    def apply_op(
        self,
        gdf: cudf.DataFrame,
        columns_ctx: dict,
        input_cols,
        target_cols=["base"],
        stats_context=None,
    ):
        target_columns = self.get_columns(columns_ctx, input_cols, target_cols)
        new_gdf = self.op_logic(gdf, target_columns, stats_context=stats_context)
        self.update_columns_ctx(columns_ctx, input_cols, new_gdf.columns, target_columns)
        return self.assemble_new_df(gdf, new_gdf, target_columns)

    def assemble_new_df(self, origin_gdf, new_gdf, target_columns):
        if not new_gdf:
            return origin_gdf
        if self.replace and self.preprocessing and target_columns:
            origin_gdf[target_columns] = new_gdf
            return origin_gdf
        return cudf.concat([origin_gdf, new_gdf], axis=1)

    def op_logic(self, gdf, target_columns, stats_context=None):
        raise NotImplementedError(
            """Must implement transform in the op_logic method,
                                     The return value must be a dataframe with all required
                                     transforms."""
        )


class DFOperator(TransformOperator):
    """
    Base class for data frame operator classes.
    """

    def required_stats(self):
        raise NotImplementedError(
            "Should consist of a list of identifiers, that should map to available statistics"
        )


class StatOperator(Operator):
    """
    Base class for statistical operator classes.
    """

    def __init__(self, columns=None):
        super(StatOperator, self).__init__(columns)

    def read_itr(
        self, gdf: cudf.DataFrame, columns_ctx: dict, input_cols, target_cols="base",
    ):
        raise NotImplementedError(
            """The operation to conduct on the dataframe to observe the desired statistics."""
        )

    def read_fin(self):
        raise NotImplementedError(
            """Upon finalization of the statistics on all data frame chunks,
                this function allows for final transformations on the statistics recorded.
                Can be 'pass' if unneeded."""
        )

    def registered_stats(self):
        raise NotImplementedError(
            """Should return a list of statistics this operator will collect.
                The list is comprised of simple string values."""
        )

    def stats_collected(self):
        raise NotImplementedError(
            """Should return a list of tuples of name and statistics operator."""
        )

    def clear(self):
        raise NotImplementedError("""zero and reinitialize all relevant statistical properties""")


class MinMax(StatOperator):
    """
    Moments operation calculates some of the statistics of features including
    mean, variance, standarded deviation, and count.

    Parameters
    -----------
    columns :
    batch_mins : list of float, default None
    batch_maxs : list of float, default None
    mins : list of float, default None
    maxs : list of float, default None
    """

    def __init__(self, columns=None, batch_mins=None, batch_maxs=None, mins=None, maxs=None):
        super().__init__(columns=columns)
        self.batch_mins = batch_mins if batch_mins is not None else {}
        self.batch_maxs = batch_maxs if batch_maxs is not None else {}
        self.mins = mins if mins is not None else {}
        self.maxs = maxs if maxs is not None else {}

    @annotate("MinMax_op", color="green", domain="nvt_python")
    def apply_op(
        self, gdf: cudf.DataFrame, columns_ctx: dict, input_cols, target_cols="base",
    ):
        """ Iteration level Min Max collection, a chunk at a time
        """
        cols = self.get_columns(columns_ctx, input_cols, target_cols)
        for col in cols:
            gdf_col = gdf[col].dropna()
            if gdf_col.dtype != "object":
                col_min = gdf_col.min()
                col_max = gdf_col.max()
            else:
                # StringColumn etc doesn't have min/max methods yet, convert
                # to host memory and take the min there.
                col_min = min(gdf_col.tolist())
                col_max = max(gdf_col.tolist())
            if col not in self.batch_mins:
                self.batch_mins[col] = []
                self.batch_maxs[col] = []
            self.batch_mins[col].append(col_min)
            self.batch_maxs[col].append(col_max)
        return

    @annotate("MinMax_fin", color="green", domain="nvt_python")
    def read_fin(self):

        for col in self.batch_mins.keys():
            # required for exporting values later,
            # must move values from gpu if cupy->numpy not supported
            self.batch_mins[col] = cudf.Series(self.batch_mins[col]).tolist()
            self.batch_maxs[col] = cudf.Series(self.batch_maxs[col]).tolist()
            self.mins[col] = min(self.batch_mins[col])
            self.maxs[col] = max(self.batch_maxs[col])
        return

    def registered_stats(self):
        return ["mins", "maxs", "batch_mins", "batch_maxs"]

    def stats_collected(self):
        result = [
            ("mins", self.mins),
            ("maxs", self.maxs),
            ("batch_mins", self.batch_mins),
            ("batch_maxs", self.batch_maxs),
        ]
        return result

    def clear(self):
        self.batch_mins = {}
        self.batch_maxs = {}
        self.mins = {}
        self.maxs = {}
        return


class Moments(StatOperator):
    """
    Moments operation calculates some of the statistics of features including
    mean, variance, standarded deviation, and count.

    Parameters
    -----------
    columns :
    counts : list of float, default None
    means : list of float, default None
    varis : list of float, default None
    stds : list of float, default None
    """

    def __init__(self, columns=None, counts=None, means=None, varis=None, stds=None):
        super().__init__(columns=columns)
        self.counts = counts if counts is not None else {}
        self.means = means if means is not None else {}
        self.varis = varis if varis is not None else {}
        self.stds = stds if stds is not None else {}

    @annotate("Moments_op", color="green", domain="nvt_python")
    def apply_op(
        self, gdf: cudf.DataFrame, columns_ctx: dict, input_cols, target_cols="base",
    ):
        """ Iteration-level moment algorithm (mean/std).
        """
        cols = self.get_columns(columns_ctx, input_cols, target_cols)
        for col in cols:
            if col not in self.counts:
                self.counts[col] = 0.0
                self.means[col] = 0.0
                self.varis[col] = 0.0
                self.stds[col] = 0.0

            # TODO: Harden this routine to handle 0-division.
            #       This algo may also break/overflow at scale.

            n1 = self.counts[col]
            n2 = float(len(gdf))

            v1 = self.varis[col]
            v2 = gdf[col].var()

            m1 = self.means[col]
            m2 = gdf[col].mean()

            self.counts[col] += n2
            self.means[col] = (m1 * n1 + m2 * n2) / self.counts[col]

            #  Variance
            t1 = n1 * v1
            t2 = n2 * v2
            t3 = n1 * ((m1 - self.means[col]) ** 2)
            t4 = n2 * ((m2 - self.means[col]) ** 2)
            t5 = n1 + n2
            self.varis[col] = (t1 + t2 + t3 + t4) / t5
        return

    @annotate("Moments_fin", color="green", domain="nvt_python")
    def read_fin(self):
        """ Finalize statistical-moments algorithm.
        """
        for col in self.varis.keys():
            self.stds[col] = float(np.sqrt(self.varis[col]))
            self.varis[col] = float(self.varis[col])
            self.means[col] = float(self.means[col])

    def registered_stats(self):
        return ["means", "stds", "vars", "counts"]

    def stats_collected(self):
        result = [
            ("means", self.means),
            ("stds", self.stds),
            ("vars", self.varis),
            ("counts", self.counts),
        ]
        return result

    def clear(self):
        self.counts = {}
        self.means = {}
        self.varis = {}
        self.stds = {}
        return


class Median(StatOperator):
    """
    This operation calculates median of features.

    Parameters
    -----------
    columns :
    fill : float, default None
    batch_medians : list, default None
    medians : list, default None
    """

    def __init__(self, columns=None, fill=None, batch_medians=None, medians=None):
        super().__init__(columns=columns)
        self.fill = fill
        self.batch_medians = batch_medians if batch_medians is not None else {}
        self.medians = medians if medians is not None else {}

    @annotate("Median_op", color="green", domain="nvt_python")
    def apply_op(
        self, gdf: cudf.DataFrame, columns_ctx: dict, input_cols, target_cols="base",
    ):
        """ Iteration-level median algorithm.
        """
        cols = self.get_columns(columns_ctx, input_cols, target_cols)
        for name in cols:
            if name not in self.batch_medians:
                self.batch_medians[name] = []
            col = gdf[name].copy()
            col = col.dropna().reset_index(drop=True).sort_values()
            if self.fill:
                self.batch_medians[name].append(self.fill)
            elif len(col) > 1:
                self.batch_medians[name].append(float(col[len(col) // 2]))
            else:
                self.batch_medians[name].append(0.0)
        return

    @annotate("Median_fin", color="green", domain="nvt_python")
    def read_fin(self, *args):
        """ Finalize median algorithm.
        """
        for col, val in self.batch_medians.items():
            self.batch_medians[col].sort()
            self.medians[col] = float(self.batch_medians[col][len(self.batch_medians[col]) // 2])
        return

    def registered_stats(self):
        return ["medians"]

    def stats_collected(self):
        result = [("medians", self.medians)]
        return result

    def clear(self):
        self.batch_medians = {}
        self.medians = {}
        return


class Encoder(StatOperator):
    """
    This is an internal operation. Encoder operation is used by
    the Categorify operation to calculate the unique numerical
    values to transform the categorical features.

    Parameters
    -----------
    use_frequency : bool
        use frequency based transformation or not.
    freq_threshold : int, default 0
        threshold value for frequency based transformation.
    limit_frac : float, default 0.5
        fraction of memory to use during unique id calculation.
    gpu_mem_util_limit : float, default 0.8
        GPU memory utilization limit during frequency based
        calculation. If limit is exceeded, unique ids are moved
        to host memory.
    gpu_mem_trans_use : float, default 0.8
        GPU memory utilization limit during transformation. How much
        GPU memory will be used during transformation is calculated
        using this parameter.
    columns :
    preprocessing : bool
    replace : bool
    """

    def __init__(
        self,
        use_frequency=False,
        freq_threshold=0,
        limit_frac=0.5,
        gpu_mem_util_limit=0.5,
        gpu_mem_trans_use=0.5,
        columns=None,
        encoders=None,
        categories=None,
    ):
        super(Encoder, self).__init__(columns)
        self.use_frequency = use_frequency
        self.freq_threshold = freq_threshold
        self.limit_frac = limit_frac
        self.gpu_mem_util_limit = gpu_mem_util_limit
        self.gpu_mem_trans_use = gpu_mem_trans_use
        self.encoders = encoders if encoders is not None else {}
        self.categories = categories if categories is not None else {}

    @annotate("Encoder_op", color="green", domain="nvt_python")
    def apply_op(
        self, gdf: cudf.DataFrame, columns_ctx: dict, input_cols, target_cols="base",
    ):
        """ Iteration-level categorical encoder update.
        """
        cols = self.get_columns(columns_ctx, input_cols, target_cols)
        if not cols:
            return
        for name in cols:
            if name not in self.encoders:
                if self.use_frequency:
                    threshold_freq = (
                        self.freq_threshold.get(name, 0)
                        if type(self.freq_threshold) is dict
                        else self.freq_threshold
                    )
                    self.encoders[name] = DLLabelEncoder(
                        name,
                        use_frequency=self.use_frequency,
                        limit_frac=self.limit_frac,
                        gpu_mem_util_limit=self.gpu_mem_util_limit,
                        # This one is used during transform
                        gpu_mem_trans_use=self.gpu_mem_trans_use,
                        freq_threshold=threshold_freq,
                    )
                else:
                    self.encoders[name] = DLLabelEncoder(name)

                gdf[name].append([None])

            self.encoders[name].fit(gdf[name])
        return

    @annotate("Encoder_fin", color="green", domain="nvt_python")
    def read_fin(self, *args):
        """ Finalize categorical encoders (get categories).
        """
        for name, val in self.encoders.items():
            self.categories[name] = val.fit_finalize()
        return

    def cat_read_all_files(self, cat_obj):
        cat_size = cat_obj.get_cats().shape[0]
        return cat_size + cat_obj.cat_exp_count

    def registered_stats(self):
        return ["encoders", "categories"]

    def stats_collected(self):
        result = [("encoders", self.encoders), ("categories", self.categories)]
        return result

    def clear(self):
        self.encoders = {}
        self.categories = {}
        return


class Export(TransformOperator):

    """
    This operation exports a dataframe to a file.

    Parameters
    -----------
    path : str, default "./ds_export"
        path to write the dataframe
    nfiles : int, default 1
        how many files to create
    shuffle : bool, default True
        shuffle the data or not
    columns :
    preprocessing : bool, default False
    replace : bool, default False
    """

    default_in = ALL
    default_out = ALL

    def __init__(
        self,
        path="./ds_export",
        nfiles=1,
        shuffle=True,
        columns=None,
        preprocessing=False,
        replace=False,
    ):
        super().__init__(columns=columns, preprocessing=preprocessing, replace=replace)
        self.path = path
        if not os.path.exists(path):
            os.makedirs(path)
        self.nfiles = nfiles
        self.shuffle = True

    @annotate("Export_op", color="darkgreen", domain="nvt_python")
    def op_logic(self, gdf: cudf.DataFrame, target_columns: list, stats_context=None):
        gdf.to_parquet(self.path, compression=None)
        return


class ZeroFill(TransformOperator):
    """
    This operation sets negative values to zero.

    Although you can directly call methods of this class to
    transform your categorical features, it's typically used within a
    Workflow class.
    """

    default_in = CONT
    default_out = CONT

    @annotate("ZeroFill_op", color="darkgreen", domain="nvt_python")
    def op_logic(self, gdf: cudf.DataFrame, target_columns: list, stats_context=None):
        cont_names = target_columns
        if not cont_names:
            return gdf
        z_gdf = gdf[cont_names].fillna(0)
        z_gdf.columns = [f"{col}_{self._id}" for col in z_gdf.columns]
        z_gdf[z_gdf < 0] = 0
        return z_gdf


class LogOp(TransformOperator):

    """
    Standardizing the features around 0 with a standard deviation
    of 1 is a common technique to compare measurements that have
    different units. This operation can be added to the workflow
    to standardize the features.

    Although you can directly call methods of this class to
    transform your categorical features, it's typically used within a
    Workflow class.
    """

    default_in = CONT
    default_out = CONT

    @annotate("LogOp_op", color="darkgreen", domain="nvt_python")
    def op_logic(self, gdf: cudf.DataFrame, target_columns: list, stats_context=None):
        cont_names = target_columns
        if not cont_names:
            return gdf
        new_gdf = np.log(gdf[cont_names].astype(np.float32) + 1)
        new_cols = [f"{col}_{self._id}" for col in new_gdf.columns]
        new_gdf.columns = new_cols
        return new_gdf


class Normalize(DFOperator):
    """
    Standardizing the features around 0 with a standard deviation
    of 1 is a common technique to compare measurements that have
    different units. This operation can be added to the workflow
    to standardize the features.

    Although you can directly call methods of this class to
    transform your categorical features, it's typically used within a
    Workflow class.
    """

    default_in = CONT
    default_out = CONT

    @property
    def req_stats(self):
        return [Moments()]

    @annotate("Normalize_op", color="darkgreen", domain="nvt_python")
    def op_logic(self, gdf: cudf.DataFrame, target_columns: list, stats_context=None):
        cont_names = target_columns
        if not cont_names or not stats_context["stds"]:
            return
        gdf = self.apply_mean_std(gdf, stats_context, cont_names)
        return gdf

    def apply_mean_std(self, gdf, stats_context, cont_names):
        new_gdf = cudf.DataFrame()
        for name in cont_names:
            if stats_context["stds"][name] > 0:
                new_col = f"{name}_{self._id}"
                new_gdf[new_col] = (gdf[name] - stats_context["means"][name]) / (
                    stats_context["stds"][name]
                )
                new_gdf[new_col] = new_gdf[new_col].astype("float32")
        return new_gdf


class FillMissing(DFOperator):

    """
    Many datasets may contain missing values for various reasons, and
    there are couple of ways to fill these missing values. This operation
    supports the top two widely used methods, namely median and constant.
    Median method calculates the median of the column (feature) and fills
    the missing values with this median value while constant method fills
    missing values with a pre-defined value.

    Although you can directly call methods of this class to
    transform your categorical features, it's typically used within a
    Workflow class.

    Parameters
    -----------
    fill_strategy : MEDIAN or CONSTANT
        MEDIAN method fills missing values with median of the column.
        CONSTANT method fills missing values with the value in fill_val
        parameter.
    fill_val : float
    filler : float
    add_col : bool, default False
    columns :
    preprocessing : bool, default True
    replace : bool, default True
    default_in :
    default_out :
    """

    MEDIAN = "median"
    CONSTANT = "constant"
    default_in = CONT
    default_out = CONT

    def __init__(
        self,
        fill_strategy=MEDIAN,
        fill_val=0,
        filler={},
        add_col=False,
        columns=None,
        preprocessing=True,
        replace=True,
        default_in=None,
        default_out=None,
    ):
        super().__init__(columns=columns, preprocessing=preprocessing, replace=replace)
        self.fill_strategy = fill_strategy
        self.fill_val = fill_val
        self.add_col = add_col
        self.filler = filler

    @property
    def req_stats(self):
        return [Median()]

    @annotate("FillMissing_op", color="darkgreen", domain="nvt_python")
    def op_logic(self, gdf: cudf.DataFrame, target_columns: list, stats_context=None):
        cont_names = target_columns
        if not cont_names or not stats_context["medians"]:
            return gdf
        z_gdf = self.apply_filler(gdf[cont_names], stats_context, cont_names)
        return z_gdf

    def apply_filler(self, gdf, stats_context, cont_names):
        na_names = [name for name in cont_names if gdf[name].isna().sum()]
        if self.add_col:
            gdf = self.add_na_indicators(gdf, na_names, cont_names)
        for col in na_names:
            gdf[col] = gdf[col].fillna(np.float32(stats_context["medians"][col]))
        gdf.columns = [f"{name}_{self._id}" for name in gdf.columns]
        return gdf

    def add_na_indicators(self, gdf: cudf.DataFrame, na_names, cat_names):
        gdf = cudf.DataFrame()
        for name in na_names:
            name_na = name + "_na"
            gdf[name_na] = gdf[name].isna()
            if name_na not in cat_names:
                cat_names.append(name_na)
        return gdf


class GroupByMoments(StatOperator):
    """
    One of the ways to create new features is to calculate
    the basic statistics of the data that is grouped by a categorical
    feature. This operator groups the data by the given categorical
    feature(s) and calculates the std, variance, and sum of requested continuous
    features along with count of every group. Then, merges these new statistics
    with the data using the unique ids of categorical data.

    Although you can directly call methods of this class to
    transform your categorical features, it's typically used within a
    Workflow class.

    Parameters
    -----------
    cat_names : list of str
        names of the categorical columns
    cont_names : list of str
        names of the continuous columns
    stats : list of str, default ['count']
        count of groups = ['count']
        sum of cont_col = ['sum']
    limit_frac : float, default 0.5
        fraction of memory to use during unique id calculation.
    gpu_mem_util_limit : float, default 0.5
        GPU memory utilization limit during frequency based
        calculation. If limit is exceeded, unique ids are moved
        to host memory.
    gpu_mem_trans_use : float, default 0.5
        GPU memory utilization limit during transformation. How much
        GPU memory will be used during transformation is calculated
        using this parameter.
    columns :
    order_column_name : str, default "order-nvtabular"
        a column name to be used to preserve the order of input data.
        cudf's merge function doesn't preserve the order of the data
        and this column name is used to create a column with integer
        values in ascending order.
    """

    def __init__(
        self,
        cat_names=None,
        cont_names=None,
        stats=["count"],
        limit_frac=0.5,
        gpu_mem_util_limit=0.5,
        gpu_mem_trans_use=0.5,
        columns=None,
        order_column_name="order-nvtabular",
    ):
        super(GroupByMoments, self).__init__(columns)
        self.cat_names = cat_names
        self.cont_names = cont_names
        self.stats = stats
        self.limit_frac = limit_frac
        self.gpu_mem_util_limit = gpu_mem_util_limit
        self.gpu_mem_trans_use = gpu_mem_trans_use
        self.order_column_name = order_column_name
        self.moments = {}
        self.categories = {}

    def apply_op(self, gdf: cudf.DataFrame, columns_ctx: dict, input_cols, target_cols="base"):
        if self.cat_names is None:
            raise ValueError("cat_names cannot be None for group by operations.")

        if self.cont_names is None:
            if "count" not in self.stats:
                raise ValueError(
                    "count operations is only supported when there is no continuous columns."
                )

        supported_ops = ["count", "sum"]
        for ops in self.stats:
            if ops not in supported_ops:
                raise ValueError(ops + " operation is not supported.")

        cols = self.get_columns(columns_ctx, input_cols, target_cols)
        if not cols:
            return
        for name in cols:
            if name not in self.cat_names:
                continue

            col_count = None
            if self.cont_names is None:
                if cols[0] == name:
                    col_count = cols[0]
                else:
                    col_count = cols[1]
                col_names = [name, col_count]
            else:
                col_count = self.cont_names[0]
                col_names = self.cont_names.copy()
                col_names.append(name)

            if name not in self.moments:
                self.moments[name] = GroupByMomentsCal(
                    col=name,
                    col_count=col_count,
                    cont_col=self.cont_names,
                    stats=self.stats,
                    limit_frac=self.limit_frac,
                    gpu_mem_util_limit=self.gpu_mem_util_limit,
                    gpu_mem_trans_use=self.gpu_mem_trans_use,
                    order_column_name=self.order_column_name,
                )

            self.moments[name].fit(gdf[col_names])
        return

    def read_fin(self, *args):
        """ Finalize categorical moments (get categories).
        """
        for name, val in self.moments.items():
            self.categories[name] = val.fit_finalize()
        return

    def registered_stats(self):
        return ["moments", "categories"]

    def stats_collected(self):
        result = [("moments", self.moments), ("categories", self.categories)]
        return result

    def clear(self):
        self.moments = {}
        self.categories = {}
        return


class GroupBy(DFOperator):
    """
    One of the ways to create new features is to calculate
    the basic statistics of the data that is grouped by a categorical
    feature. This operator groups the data by the given categorical
    feature(s) and calculates the std, variance, and sum of requested continuous
    features along with count of every group. Then, merges these new statistics
    with the data using the unique ids of categorical data.

    Although you can directly call methods of this class to
    transform your categorical features, it's typically used within a
    Workflow class.

    Parameters
    -----------
    cat_names : list of str
        names of the categorical columns
    cont_names : list of str
        names of the continuous columns
    stats : list of str, default ['count']
        count of groups = ['count']
        sum of cont_col = ['sum']
    limit_frac : float, default 0.5
        fraction of memory to use during unique id calculation.
    gpu_mem_util_limit : float, default 0.5
        GPU memory utilization limit during frequency based
        calculation. If limit is exceeded, unique ids are moved
        to host memory.
    gpu_mem_trans_use : float, default 0.5
        GPU memory utilization limit during transformation. How much
        GPU memory will be used during transformation is calculated
        using this parameter.
    columns :
    preprocessing : bool, default True
        Sets if this is a pre-processing operation or not
    replace : bool, default False
        This parameter is ignored
    order_column_name : str, default "order-nvtabular"
        a column name to be used to preserve the order of input data.
        cudf's merge function doesn't preserve the order of the data
        and this column name is used to create a column with integer
        values in ascending order.
    """

    default_in = CAT
    default_out = CAT

    def __init__(
        self,
        cat_names=None,
        cont_names=None,
        stats=["count"],
        limit_frac=0.5,
        gpu_mem_util_limit=0.5,
        gpu_mem_trans_use=0.5,
        columns=None,
        preprocessing=True,
        replace=False,
        order_column_name="order-nvtabular",
    ):
        super().__init__(columns=columns, preprocessing=preprocessing, replace=False)
        self.cat_names = cat_names
        self.cont_names = cont_names
        self.stats = stats
        self.order_column_name = order_column_name
        self.limit_frac = limit_frac
        self.gpu_mem_util_limit = gpu_mem_util_limit
        self.gpu_mem_trans_use = gpu_mem_trans_use

    @property
    def req_stats(self):
        return [
            GroupByMoments(
                cat_names=self.cat_names,
                cont_names=self.cont_names,
                stats=self.stats,
                limit_frac=self.limit_frac,
                gpu_mem_util_limit=self.gpu_mem_util_limit,
                gpu_mem_trans_use=self.gpu_mem_trans_use,
                order_column_name=self.order_column_name,
            )
        ]

    def op_logic(self, gdf: cudf.DataFrame, target_columns: list, stats_context=None):
        if self.cat_names is None:
            raise ValueError("cat_names cannot be None.")

        new_gdf = cudf.DataFrame()
        for name in stats_context["moments"]:
            tran_gdf = stats_context["moments"][name].merge(gdf)
            new_gdf[tran_gdf.columns] = tran_gdf

        return new_gdf


class Categorify(DFOperator):

    """
    Most of the data set will contain categorical features,
    and these variables are typically stored as text values.
    Machine Learning algorithms don't support these text values.
    Categorify operation can be added to the workflow to
    transform categorical features into unique integer values.

    Although you can directly call methods of this class to
    transform your categorical features, it's typically used within a
    Workflow class.

    Parameters
    -----------
    use_frequency : bool
        freq
    freq_threshold : float
        threshold
    limit_frac : float, default 0.5
        fraction of memory to use during unique id calculation.
    gpu_mem_util_limit : float, default 0.5
        GPU memory utilization limit during frequency based
        calculation. If limit is exceeded, unique ids are moved
        to host memory.
    gpu_mem_trans_use : float, default 0.5
        GPU memory utilization limit during transformation. How much
        GPU memory will be used during transformation is calculated
        using this parameter.
    columns :
    preprocessing : bool, default True
        Sets if this is a pre-processing operation or not
    replace : bool, default True
        Replaces the transformed column with the original input
        if set Yes
    cat_names :
    embed_sz :
    """

    default_in = CAT
    default_out = CAT

    def __init__(
        self,
        use_frequency=False,
        freq_threshold=0,
        limit_frac=0.5,
        gpu_mem_util_limit=0.5,
        gpu_mem_trans_use=0.5,
        columns=None,
        preprocessing=True,
        replace=True,
        cat_names=None,
        embed_sz=None,
    ):
        super().__init__(columns=columns, preprocessing=preprocessing, replace=replace)
        self.use_frequency = use_frequency
        self.freq_threshold = freq_threshold
        self.limit_frac = limit_frac
        self.gpu_mem_util_limit = gpu_mem_util_limit
        self.gpu_mem_trans_use = gpu_mem_trans_use
        self.cat_names = cat_names if cat_names else []
        self.embed_sz = embed_sz if embed_sz else {}

    @property
    def req_stats(self):
        return [
            Encoder(
                use_frequency=self.use_frequency,
                freq_threshold=self.freq_threshold,
                limit_frac=self.limit_frac,
                gpu_mem_util_limit=self.gpu_mem_util_limit,
                gpu_mem_trans_use=self.gpu_mem_trans_use,
            )
        ]

    @annotate("Categorify_op", color="darkgreen", domain="nvt_python")
    def op_logic(self, gdf: cudf.DataFrame, target_columns: list, stats_context=None):
        cat_names = target_columns
        new_gdf = cudf.DataFrame()
        if not cat_names:
            return gdf
        cat_names = [name for name in cat_names if name in gdf.columns]
        new_cols = []
        for name in cat_names:
            new_col = f"{name}_{self._id}"
            new_cols.append(new_col)
            new_gdf[new_col] = stats_context["encoders"][name].transform(gdf[name])
            new_gdf[new_col] = new_gdf[new_col].astype("int64")
        return new_gdf

    def get_emb_sz(self, encoders, cat_names):
        # sorted key required to ensure same sort occurs for all values
        ret_list = [
            (n, self.def_emb_sz(encoders, n))
            for n in sorted(cat_names, key=lambda entry: entry.split("_")[0])
        ]
        return ret_list

    def emb_sz_rule(self, n_cat: int) -> int:
        return min(16, round(1.6 * n_cat ** 0.56))

    def def_emb_sz(self, classes, n, sz_dict=None):
        """Pick an embedding size for `n` depending on `classes` if not given in `sz_dict`.
        """
        sz_dict = sz_dict if sz_dict else {}
        n_cat = classes[n]
        sz = sz_dict.get(n, int(self.emb_sz_rule(n_cat)))  # rule of thumb
        self.embed_sz[n] = sz
        return n_cat, sz


all_ops = {
    MinMax()._id: MinMax,
    Moments()._id: Moments,
    Median()._id: Median,
    Encoder()._id: Encoder,
    Export()._id: Export,
    ZeroFill()._id: ZeroFill,
    LogOp()._id: LogOp,
    Normalize()._id: Normalize,
    FillMissing()._id: FillMissing,
    Categorify()._id: Categorify,
    GroupBy()._id: GroupBy,
    GroupByMoments()._id: GroupByMoments,
}
