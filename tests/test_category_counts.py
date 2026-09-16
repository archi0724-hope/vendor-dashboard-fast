import pandas as pd
from vendor_core import DOCUMENT_TYPES, document_type_counts


def test_empty_categories_are_visible():
    result = document_type_counts(pd.DataFrame())
    assert result['Document type'].tolist() == DOCUMENT_TYPES
    assert result.Documents.sum() == result.Companies.sum() == 0


def test_counts_files_separately_from_companies_and_excludes_unavailable():
    docs = pd.DataFrame([
        {'company_key': 'alpha', 'types': ['GST', 'GST', 'ASF ISO'], 'available': True},
        {'company_key': 'alpha', 'types': ['GST'], 'available': True},
        {'company_key': 'beta', 'types': ['GST', 'Cancelled Cheque'], 'available': True},
        {'company_key': 'beta', 'types': ['Udyam'], 'available': False},
        {'company_key': '', 'types': ['PAN Card'], 'available': True},
        {'company_key': 'beta', 'types': ['Other'], 'available': True},
    ])
    result = document_type_counts(docs).set_index('Document type')
    assert result.loc['GST'].tolist() == [3, 2]
    assert result.loc['ASF ISO'].tolist() == [1, 1]
    assert result.loc['Cancelled Cheque'].tolist() == [1, 1]
    assert result.loc['Udyam'].tolist() == [0, 0]
    assert result.loc['PAN Card'].tolist() == [1, 0]
