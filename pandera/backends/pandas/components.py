"""Backend implementation for pandas schema components."""

import traceback
from copy import copy, deepcopy
from typing import Iterable, Optional, Union

import numpy as np
import pandas as pd

from pandera.backends.pandas.array import ArraySchemaBackend
from pandera.backends.pandas.container import DataFrameSchemaBackend
from pandera.api.pandas.types import (
    is_field,
    is_index,
    is_multiindex,
    is_table,
)
from pandera.backends.pandas.error_formatters import scalar_failure_case
from pandera.error_handlers import SchemaErrorHandler
from pandera.errors import SchemaError, SchemaErrors


class ColumnBackend(ArraySchemaBackend):
    """Backend implementation for pandas dataframe columns."""

    def validate(
        self,
        check_obj: pd.DataFrame,
        schema,
        *,
        head: Optional[int] = None,
        tail: Optional[int] = None,
        sample: Optional[int] = None,
        random_state: Optional[int] = None,
        lazy: bool = False,
        inplace: bool = False,
    ) -> pd.DataFrame:
        """Validation backend implementation for pandas dataframe columns.."""
        if not inplace:
            check_obj = check_obj.copy()

        error_handler = SchemaErrorHandler(lazy=lazy)

        if schema.name is None:
            raise SchemaError(
                schema,
                check_obj,
                "column name is set to None. Pass the ``name` argument when "
                "initializing a Column object, or use the ``set_name`` "
                "method.",
            )

        def validate_column(check_obj, column_name):
            try:
                # pylint: disable=super-with-arguments
                super(ColumnBackend, self).validate(
                    check_obj,
                    copy(schema).set_name(column_name),
                    head=head,
                    tail=tail,
                    sample=sample,
                    random_state=random_state,
                    lazy=lazy,
                    inplace=inplace,
                )
            except SchemaErrors as err:
                for err_dict in err.schema_errors:
                    error_handler.collect_error(
                        err_dict["reason_code"], err_dict["error"]
                    )
            except SchemaError as err:
                error_handler.collect_error(err.reason_code, err)

        column_keys_to_check = (
            self.get_regex_columns(schema, check_obj.columns)
            if schema.regex
            else [schema.name]
        )

        for column_name in column_keys_to_check:
            if schema.coerce:
                check_obj[column_name] = self.coerce_dtype(
                    check_obj[column_name],
                    schema=schema,
                    error_handler=error_handler,
                )

            if is_table(check_obj[column_name]):
                for i in range(check_obj[column_name].shape[1]):
                    validate_column(
                        check_obj[column_name].iloc[:, [i]], column_name
                    )
            else:
                validate_column(check_obj, column_name)

        if lazy and error_handler.collected_errors:
            raise SchemaErrors(
                schema=schema,
                schema_errors=error_handler.collected_errors,
                data=check_obj,
            )

        return check_obj

    def get_regex_columns(
        self, schema, columns: Union[pd.Index, pd.MultiIndex]
    ) -> Iterable:
        """Get matching column names based on regex column name pattern.

        :param schema: schema specification to use
        :param columns: columns to regex pattern match
        :returns: matchin columns
        """
        if isinstance(schema.name, tuple):
            # handle MultiIndex case
            if len(schema.name) != columns.nlevels:
                raise IndexError(
                    f"Column regex name='{schema.name}' is a tuple, expected a "
                    f"MultiIndex columns with {len(schema.name)} number of "
                    f"levels, found {columns.nlevels} level(s)"
                )
            matches = np.ones(len(columns)).astype(bool)
            for i, name in enumerate(schema.name):
                matched = pd.Index(
                    columns.get_level_values(i).astype(str).str.match(name)
                ).fillna(False)
                matches = matches & np.array(matched.tolist())
            column_keys_to_check = columns[matches]
        else:
            if is_multiindex(columns):
                raise IndexError(
                    f"Column regex name {schema.name} is a string, expected a "
                    "dataframe where the index is a pd.Index object, not a "
                    "pd.MultiIndex object"
                )
            column_keys_to_check = columns[
                # str.match will return nan values when the index value is
                # not a string.
                pd.Index(columns.astype(str).str.match(schema.name))
                .fillna(False)
                .tolist()
            ]
        if column_keys_to_check.shape[0] == 0:
            raise SchemaError(
                schema=schema,
                data=columns,
                message=(
                    f"Column regex name='{schema.name}' did not match any "
                    "columns in the dataframe. Update the regex pattern so "
                    f"that it matches at least one column:\n{columns.tolist()}",
                ),
                failure_cases=scalar_failure_case(str(columns.tolist())),
                check=f"no_regex_column_match('{schema.name}')",
            )
        # drop duplicates to account for potential duplicated columns in the
        # dataframe.
        return column_keys_to_check.drop_duplicates()

    def coerce_dtype(
        self,
        check_obj: Union[pd.DataFrame, pd.Series],
        *,
        schema=None,
        error_handler: SchemaErrorHandler = None,
    ) -> Union[pd.DataFrame, pd.Series]:
        """Coerce dtype of a column, handling duplicate column names."""
        # pylint: disable=super-with-arguments
        # pylint: disable=fixme
        # TODO: use singledispatchmethod here
        if is_field(check_obj) or is_index(check_obj):
            return super(ColumnBackend, self).coerce_dtype(
                check_obj,
                schema=schema,
                error_handler=error_handler,
            )
        return check_obj.apply(
            lambda x: super(ColumnBackend, self).coerce_dtype(
                x,
                schema=schema,
                error_handler=error_handler,
            ),
            axis="columns",
        )

    def run_checks(self, check_obj, schema, error_handler, lazy):
        check_results = []
        for check_index, check in enumerate(schema.checks):
            check_args = [None] if is_field(check_obj) else [schema.name]
            try:
                check_results.append(
                    self.run_check(
                        check_obj, schema, check, check_index, *check_args
                    )
                )
            except SchemaError as err:
                error_handler.collect_error("dataframe_check", err)
            except Exception as err:  # pylint: disable=broad-except
                # catch other exceptions that may occur when executing the Check
                err_msg = f'"{err.args[0]}"' if len(err.args) > 0 else ""
                err_str = f"{err.__class__.__name__}({ err_msg})"
                error_handler.collect_error(
                    "check_error",
                    SchemaError(
                        schema=schema,
                        data=check_obj,
                        message=(
                            f"Error while executing check function: {err_str}\n"
                            + traceback.format_exc()
                        ),
                        failure_cases=scalar_failure_case(err_str),
                        check=check,
                        check_index=check_index,
                    ),
                    original_exc=err,
                )
        return check_results


class IndexBackend(ArraySchemaBackend):
    """Backend implementation for pandas index."""

    def validate(
        self,
        check_obj: Union[pd.DataFrame, pd.Series],
        schema,
        *,
        head: Optional[int] = None,
        tail: Optional[int] = None,
        sample: Optional[int] = None,
        random_state: Optional[int] = None,
        lazy: bool = False,
        inplace: bool = False,
    ) -> Union[pd.DataFrame, pd.Series]:
        if is_multiindex(check_obj.index):
            raise SchemaError(
                schema, check_obj, "Attempting to validate mismatch index"
            )

        series_cls = pd.Series
        # NOTE: this is a hack to get pyspark.pandas working, this needs a more
        # principled implementation
        if type(check_obj).__module__ == "pyspark.pandas.frame":
            # pylint: disable=import-outside-toplevel
            import pyspark.pandas as ps

            series_cls = ps.Series

        if schema.coerce:
            check_obj.index = schema.coerce_dtype(check_obj.index)
            # handles case where pandas native string type is not supported
            # by index.
            obj_to_validate = schema.dtype.coerce(
                series_cls(
                    check_obj.index.to_numpy(), name=check_obj.index.name
                )
            )
        else:
            obj_to_validate = series_cls(
                check_obj.index.to_numpy(), name=check_obj.index.name
            )

        assert is_field(
            super().validate(
                obj_to_validate,
                schema,
                head=head,
                tail=tail,
                sample=sample,
                random_state=random_state,
                lazy=lazy,
                inplace=inplace,
            ),
        )
        return check_obj


class MultiIndexBackend(DataFrameSchemaBackend):
    """Backend implementation for pandas multiindex."""

    def coerce_dtype(  # type: ignore[override]
        self,
        # pylint: disable=fixme
        # TODO: make MultiIndex not inherit from DataFrameSchemaBackend
        check_obj: pd.MultiIndex,
        *,
        schema=None,
        error_handler: SchemaErrorHandler = None,
    ) -> pd.MultiIndex:
        """Coerce type of a pd.Series by type specified in dtype.

        :param obj: multi-index to coerce.
        :returns: ``MultiIndex`` with coerced data type
        """
        assert schema is not None, "The `schema` argument must be provided."

        if not schema.coerce:
            return check_obj

        error_handler = SchemaErrorHandler(lazy=True)

        # construct MultiIndex with coerced data types
        coerced_multi_index = {}
        for i, index in enumerate(schema.indexes):
            if all(x is None for x in schema.names):
                index_levels = [i]
            else:
                index_levels = [
                    i
                    for i, name in enumerate(check_obj.names)
                    if name == index.name
                ]
            for index_level in index_levels:
                index_array = check_obj.get_level_values(index_level)
                if index.coerce or schema._coerce:
                    try:
                        _index = deepcopy(index)
                        _index.coerce = True
                        index_array = _index.coerce_dtype(index_array)
                    except SchemaError as err:
                        error_handler.collect_error(
                            "dtype_coercion_error", err
                        )
                coerced_multi_index[index_level] = index_array

        if error_handler.collected_errors:
            raise SchemaErrors(
                schema=schema,
                schema_errors=error_handler.collected_errors,
                data=check_obj,
            )

        multiindex_cls = pd.MultiIndex
        # NOTE: this is a hack to support pyspark.pandas
        if type(check_obj).__module__.startswith("pyspark.pandas"):
            # pylint: disable=import-outside-toplevel
            import pyspark.pandas as ps

            multiindex_cls = ps.MultiIndex

        return multiindex_cls.from_arrays(
            [
                # v.dtype may be different than 'object'.
                # - Reuse the original index array to keep the specialized dtype:
                #   v.to_numpy()  converts the array dtype to array of 'object' dtype.
                #   Thus removing the specialized index dtype required to pass a schema's
                #   index specialized dtype : eg: pandera.typing.Index(pandas.Int64Dtype)
                # - For Pyspark only, use to_numpy(), with the effect of keeping the
                #   bug open on this execution environment: At the time of writing, pyspark
                #   v3.3.0 does not provide a working implementation of v.array
                v.to_numpy()
                if type(v).__module__.startswith("pyspark.pandas")
                else v.array
                for _, v in sorted(
                    coerced_multi_index.items(), key=lambda x: x[0]
                )
            ],
            names=check_obj.names,
        )

    def validate(
        self,
        check_obj: Union[pd.DataFrame, pd.Series],
        schema,
        *,
        head: Optional[int] = None,
        tail: Optional[int] = None,
        sample: Optional[int] = None,
        random_state: Optional[int] = None,
        lazy: bool = False,
        inplace: bool = False,
    ) -> Union[pd.DataFrame, pd.Series]:
        """Validate DataFrame or Series MultiIndex.

        :param check_obj: pandas DataFrame of Series to validate.
        :param head: validate the first n rows. Rows overlapping with `tail` or
            `sample` are de-duplicated.
        :param tail: validate the last n rows. Rows overlapping with `head` or
            `sample` are de-duplicated.
        :param sample: validate a random sample of n rows. Rows overlapping
            with `head` or `tail` are de-duplicated.
        :param random_state: random seed for the ``sample`` argument.
        :param lazy: if True, lazily evaluates dataframe against all validation
            checks and raises a ``SchemaErrors``. Otherwise, raise
            ``SchemaError`` as soon as one occurs.
        :param inplace: if True, applies coercion to the object of validation,
            otherwise creates a copy of the data.
        :returns: validated DataFrame or Series.
        """
        # pylint: disable=too-many-locals
        if schema.coerce:
            try:
                check_obj.index = self.coerce_dtype(
                    check_obj.index, schema=schema  # type: ignore [arg-type]
                )
            except SchemaErrors as err:
                if lazy:
                    raise
                raise err.schema_errors[0]["error"] from err

        # Prevent data type coercion when the validate method is called because
        # it leads to some weird behavior when calling coerce_dtype within the
        # DataFrameSchema.validate call. Need to fix this by having MultiIndex
        # not inherit from DataFrameSchema.
        schema_copy = deepcopy(schema)
        schema_copy.coerce = False
        for index in schema_copy.indexes:
            index.coerce = False

        # rename integer-based column names in case of duplicate index names,
        # with at least one named index.
        if (
            not all(x is None for x in check_obj.index.names)
            and len(set(check_obj.index.names)) != check_obj.index.nlevels
        ):
            index_names = []
            for i, name in enumerate(check_obj.index.names):
                name = i if name is None else name
                if name not in index_names:
                    index_names.append(name)

            columns = {}
            for name, (_, column) in zip(
                index_names, schema_copy.columns.items()
            ):
                columns[name] = column.set_name(name)
            schema_copy.columns = columns

        def to_dataframe(multiindex):
            """
            Emulate the behavior of pandas.MultiIndex.to_frame, but preserve
            duplicate index names if they exist.
            """
            # NOTE: this is a hack to support pyspark.pandas
            if type(multiindex).__module__.startswith("pyspark.pandas"):
                df = multiindex.to_frame()
            else:
                df = pd.DataFrame(
                    {
                        i: multiindex.get_level_values(i)
                        for i in range(multiindex.nlevels)
                    }
                )
                df.columns = [
                    i if name is None else name
                    for i, name in enumerate(multiindex.names)
                ]
                df.index = multiindex
            return df

        try:
            validation_result = super().validate(
                to_dataframe(check_obj.index),
                schema_copy,
                head=head,
                tail=tail,
                sample=sample,
                random_state=random_state,
                lazy=lazy,
                inplace=inplace,
            )
        except SchemaErrors as err:
            # This is a hack to re-raise the SchemaErrors exception and change
            # the schema context to MultiIndex. This should be fixed by with
            # a more principled schema class hierarchy.
            schema_error_dicts = []
            for schema_error_dict in err.schema_errors:
                error = schema_error_dict["error"]
                error = SchemaError(
                    schema,
                    check_obj,
                    error.args[0],
                    error.failure_cases.assign(column=error.schema.name),
                    error.check,
                    error.check_index,
                )
                schema_error_dict["error"] = error
                schema_error_dicts.append(schema_error_dict)

            raise SchemaErrors(
                schema=schema,
                schema_errors=schema_error_dicts,
                data=check_obj,
            ) from err

        assert is_table(validation_result)
        return check_obj
