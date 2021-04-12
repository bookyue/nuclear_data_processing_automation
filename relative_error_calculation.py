from decimal import localcontext, Decimal, InvalidOperation

import numpy as np
import pandas as pd

from db.db_model import PhysicalQuantity, File
from db.fetch_data import (fetch_extracted_data_by_filename_and_physical_quantity,
                           fetch_files_by_name,
                           fetch_physical_quantities_by_name, fetch_extracted_data_id)
from utils.configlib import config
from utils.formatter import type_checker
from utils.workbook import append_df_to_excel


def _complement_columns(df_reference,
                        df_comparison,
                        reference_complement_column_name,
                        comparison_complement_column_name):
    """
    对齐middle_step_* columns，数值填充为NaN

    Parameters
    ----------
    df_reference : pd.DataFrame
    df_comparison : pd.DataFrame
    reference_complement_column_name : str
    comparison_complement_column_name : str

    Returns
    -------
    tuple[pd.DataFrame, pd.DataFrame]
    """
    reference_column_length = len(df_reference.columns)
    comparison_column_length = len(df_comparison.columns)
    column_length_difference = reference_column_length - comparison_column_length

    column_start_num = min(reference_column_length, comparison_column_length) - 2

    if reference_column_length == comparison_column_length:
        # 当column length相同时，什么也不做
        pass
    elif reference_column_length < comparison_column_length:
        complement_columns = [f'{reference_complement_column_name}_middle_step_{i}'
                              for i in range(column_start_num,
                                             column_start_num + abs(column_length_difference))]
        complement_df = pd.DataFrame(data=None, columns=complement_columns)
        df_reference = pd.concat([df_reference, complement_df], axis=1, copy=False)
    else:
        complement_columns = [f'{comparison_complement_column_name}_middle_step_{i}'
                              for i in range(column_start_num,
                                             column_start_num + abs(column_length_difference))]
        complement_df = pd.DataFrame(data=None, columns=complement_columns)
        df_comparison = pd.concat([df_comparison, complement_df], axis=1, copy=False)

    return df_reference, df_comparison


def _calculate_deviation(df_reference,
                         df_comparison,
                         deviation_mode='relative',
                         threshold=Decimal('1.0E-12')):
    """
    计算误差
    relative deviation formula: abs(X - Y) / 1 + min(abs(X), abs(Y))
    absolute deviation formula: X - Y

    Parameters
    ----------
    df_reference : pd.DataFrame
    df_comparison : pd.DataFrame
    deviation_mode : str, default = 'relative'
        绝对=absolute
        相对=relative
        偏差模式，分为绝对和相对，默认为相对

    Returns
    -------
    tuple[pd.DataFrame, pd.Series]
    """

    df_deviation = pd.DataFrame()
    for reference_column, \
        comparison_column in zip(df_reference.columns.values.tolist()[2:],
                                 df_comparison.columns.values.tolist()[2:]):
        i = 0
        if deviation_mode == 'relative':
            # abs(X - Y) / 1 + min(abs(X), abs(Y))
            """NaN is classified as unordered.
            By default InvalidOperation is trapped, 
            so a Python exception is raised when using <= and >= against Decimal('NaN'). 
            This is a logical extension; 
            Python has actual exceptions so if you compare against the 
            NaN exception value, you can expect an exception being raised. 
            You could disable trapping by using a Decimal.localcontext()
            https://stackoverflow.com/a/28371465/11071374
            """
            with localcontext() as ctx:
                ctx.traps[InvalidOperation] = False
                deviation = (df_reference[reference_column] - df_comparison[comparison_column]).abs() / \
                            (1 + np.minimum(df_reference[reference_column].fillna(Decimal('NaN')),
                                            df_comparison[comparison_column].fillna(Decimal('NaN'))))

        elif deviation_mode == 'absolute':
            deviation = (df_reference[reference_column] - df_comparison[comparison_column]).abs()

        else:
            raise Exception("wrong deviation mode")

        if not deviation.isnull().values.all():
            df_deviation[f'relative_deviation_middle_step_{i}'] = deviation

        i += 1

    df_deviation.rename(columns={'relative_deviation_middle_step_0': 'relative_deviation_last_step'}, inplace=True)

    reserved_index = (df_deviation.T > threshold).any()
    df_deviation = df_deviation.loc[reserved_index].reset_index(drop=True)

    return df_deviation, reserved_index


def _merge_reference_comparison_and_deviation(df_reference,
                                              df_comparison,
                                              df_deviation,
                                              reserved_index,
                                              reserved_na=False):
    """
    依据reserved_index合并reference，comparison和deviation表
    如果reserved_na为False,则drop columns with all NaN's，否则反之
    默认reserved_na为False

    Parameters
    ----------
    df_reference : pd.DataFrame
    df_comparison : pd.DataFrame
    df_deviation : pd.DataFrame
    reserved_index : pd.Series
    reserved_na : bool, default = False
        如果reserved_na为False,则drop columns with all NaN's，否则反之
    Returns
    -------
    pd.DataFrame
    """
    df_ = pd.merge(df_reference.loc[reserved_index],
                   df_comparison.loc[reserved_index],
                   how='outer', on=['nuc_ix', 'name'])

    df_all = pd.concat([df_, df_deviation], axis=1, copy=False)

    if not reserved_na:
        df_all.dropna(axis=1, how='all', inplace=True)

    return df_all


def save_to_excel(dict_df_all,
                  reference_file_name,
                  comparison_file_name,
                  dir_path,
                  is_all_step=False):
    """
    保存结果至xlsx文件
    
    Parameters
    ----------
    dict_df_all : dict[str, pd.DataFrame]
    reference_file_name : str
    comparison_file_name : str
    dir_path : Path
    is_all_step : bool, default = False
        是否读取过全部中间结果数据列，默认只读取最终结果列
    Returns
    -------

    """
    if is_all_step:
        file_name = f'all_step_{reference_file_name}_vs_{comparison_file_name}.xlsx'
    else:
        file_name = f'{reference_file_name}_vs_{comparison_file_name}.xlsx'

    dir_path = dir_path.joinpath(reference_file_name)
    dir_path.mkdir(parents=True, exist_ok=True)

    file_path = dir_path.joinpath(file_name)

    file_path.unlink(missing_ok=True)

    for physical_quantity_name in dict_df_all:
        append_df_to_excel(file_path, dict_df_all[physical_quantity_name],
                           sheet_name=physical_quantity_name,
                           index=False,
                           encoding='utf-8'
                           )


def calculate_comparative_result(nuc_data_id,
                                 reference_file,
                                 comparison_file,
                                 physical_quantities='isotope',
                                 deviation_mode='relative',
                                 threshold=Decimal('1.0E-12'),
                                 is_all_step=False):
    """
    选定一个基准文件，一个对比文件，与其进行对比，计算并输出对比结果至工作簿(xlsx文件)

    Parameters
    ----------
    nuc_data_id : list[int]
    reference_file : File or str
        基准文件
    comparison_file : File or str
        对比文件
    physical_quantities : list[str or PhysicalQuantity] or str or PhysicalQuantity, default = 'isotope'
        对比用物理量，可以是物理量名的list[str]或str，
        也可以是PhysicalQuantity list也可以是list[PhysicalQuantity]或PhysicalQuantity
        默认为核素密度
    deviation_mode : str, default = 'relative'
        绝对=absolute
        相对=relative
        偏差模式，分为绝对和相对，默认为相对
    threshold : Decimal, default = Decimal('1.0E-12')
        偏差阈值，默认1.0E-12
    is_all_step : bool, default = False
        是否读取全部中间结果数据列，默认只读取最终结果列
    Returns
    -------

    """

    if type_checker([reference_file, comparison_file], File) == 'str':
        reference_file = fetch_files_by_name(reference_file).pop()
        comparison_file = fetch_files_by_name(comparison_file).pop()

    if type_checker(physical_quantities, PhysicalQuantity) == 'str':
        physical_quantities = fetch_physical_quantities_by_name(physical_quantities)

    dict_df_all = {}

    physical_quantity: PhysicalQuantity
    for physical_quantity in physical_quantities:
        reference_data = fetch_extracted_data_by_filename_and_physical_quantity(nuc_data_id,
                                                                                reference_file,
                                                                                physical_quantity,
                                                                                is_all_step)

        comparison_data = fetch_extracted_data_by_filename_and_physical_quantity(nuc_data_id,
                                                                                 comparison_file,
                                                                                 physical_quantity,
                                                                                 is_all_step
                                                                                 )

        if reference_data.empty or comparison_data.empty:
            continue

        reference_data, comparison_data = _complement_columns(reference_data,
                                                              comparison_data,
                                                              reference_file.name,
                                                              comparison_file.name)

        df_deviation, reserved_index = _calculate_deviation(reference_data,
                                                            comparison_data,
                                                            deviation_mode,
                                                            Decimal(threshold))

        dict_df_all[physical_quantity.name] = _merge_reference_comparison_and_deviation(reference_data,
                                                                                        comparison_data,
                                                                                        df_deviation,
                                                                                        reserved_index)

    return dict_df_all


def main():
    filenames = fetch_files_by_name()
    physical_quantities = fetch_physical_quantities_by_name('all')

    fission_light_nuclide_list = config.get_nuclide_list('fission_light')

    nuc_data_id = fetch_extracted_data_id(filenames,
                                          physical_quantities,
                                          fission_light_nuclide_list)

    result_path = config.get_file_path('result_file_path')

    dict_df_all = calculate_comparative_result(nuc_data_id=nuc_data_id,
                                               reference_file='001',
                                               comparison_file='002',
                                               physical_quantities='all',
                                               is_all_step=True)
    save_to_excel(dict_df_all,
                  '001',
                  '002',
                  result_path,
                  is_all_step=True)


if __name__ == '__main__':
    main()
