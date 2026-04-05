"""
1. Download file from the link
2. Open the file and read all data
3. Plot a graph

# TODO:
√ - total number of employees - Summary
- All other years comparison

"""
import os
import requests
import time
import pandas as pd
import matplotlib.pyplot as plt
from datetime import datetime
from numpy import arange
from glob import glob


def download_file_from_url(url, filename):
    """

    :type url: str
    :type filename: str
    """
    if os.path.isfile(filename):
        os.remove(filename)
    response = requests.get(url)
    with open(filename, 'wb') as f:
        f.write(response.content)
    return filename


def main():
    url = 'https://edd.ca.gov/siteassets/files/jobs_and_training/warn/warn_report.xlsx'
    filename = 'file.xlsx'

    download_file_from_url(url=url, filename=filename)

    dfs = pd.read_excel(filename, sheet_name=None, parse_dates=True)

    df = dfs['Sheet1']
    col_effective_date = "Effective \nDate"
    col_company = "Company"
    col_no_of_employees = 'No. Of\nEmployees'

    # df_tail = df[col_no_of_employees].tail(2)
    df[col_no_of_employees].tail(2)

    df_tail_2 = df.tail(2)
    col_employees_affected = "Notice\nDate"

    summary_text = ""
    for index, row in df_tail_2.iterrows():
        summary_text += " " + str(row[col_employees_affected])

    df.drop(df.tail(2).index, inplace=True)

    final_dict = dict()

    for index, row in df.iterrows():
        if '&rsquo;' in row[col_company]:
            temp_list = row[col_company].split('&rsquo;')
            company_name = temp_list[0] + "'" + temp_list[-1]
        else:
            company_name = row[col_company]

        if row[col_effective_date] not in final_dict:
            final_dict[row[col_effective_date]] = [(company_name, row[col_no_of_employees])]
        else:
            counter = False
            for i in range(len(final_dict[row[col_effective_date]])):
                if final_dict[row[col_effective_date]][i][0] == company_name:
                    no_of_employees = int(final_dict[row[col_effective_date]][i][1]) + int(row[col_no_of_employees])
                    final_dict[row[col_effective_date]][i] = (company_name, no_of_employees)
                    counter = True
                    break
            if not counter:
                final_dict[row[col_effective_date]].append((company_name, row[col_no_of_employees]))

    x_values = []
    y_values = []
    x_labels = []

    for k, v in final_dict.items():
        for item in v:
            x_values.append(k)
            y_values.append(item[1])
            x_labels.append(item[0])

    print(final_dict)
    print(x_values)
    print(x_labels)
    print(y_values)

    color_list = ["blue", "orange", "green", "red", "purple", "brown", "pink", "gray", "olive", "cyan", "black",
                  "darkred", "gold", "peru", "lime"]*10

    fig, ax = plt.subplots(figsize=(30, 10))
    ax.scatter(x_values, y_values, alpha=0.7, marker="x", s=2, linewidths=3, c="red")

    plt.xticks(x_values,
               rotation='vertical',
               fontsize=6)
    for i, txt in enumerate(x_labels):
        ax.annotate(txt, (x_values[i], y_values[i]), va='bottom', rotation=45, c=color_list[i])

    plt.tight_layout()
    plt.title(f"California:{summary_text}")

    plt.savefig(os.path.join(os.getcwd(),
                             f'graph_{time.strftime("%Y%m%d_%H%M%S")}.png'), dpi=300, bbox_inches='tight')
    plt.show()


if __name__ == '__main__':
    main()
