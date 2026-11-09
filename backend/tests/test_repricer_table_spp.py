from datetime import date
from io import BytesIO
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import pytest

from app.repricer_table_export import COLUMNS, render_repricer_table, table_rows


def item(base=430000, buyer=142600):
    return {'meta': {'nmId': 1025784485, 'currentPriceKopecks': 206400},
            'analytics': {'basePriceKopecks': base, 'buyerPriceNoWalletKopecks': buyer,
                          'sppPct': 30.91, 'buyerPriceWithWalletKopecks': 100000}}


@pytest.mark.parametrize('base,buyer,expected', [
    (430000, 142600, 0.668), (100000, 33151, 0.668), (100000, 100000, 0),
    (None, 142600, None), (430000, None, None), (0, 0, None), (100000, 100001, None),
])
def test_reporting_spp_uses_base_price_and_preserves_missingness(base, buyer, expected):
    actual = dict(zip([c[0] for c in COLUMNS], table_rows([item(base, buyer)])[0]))
    assert actual['% СПП'] == (pytest.approx(expected) if expected is not None else None)
    assert actual['Цена до СПП'] == (buyer / 100 if buyer is not None and base is not None and 0 < buyer <= base else None)
    assert 'С СПП' not in actual and 'С кошельком' not in actual


def test_xlsx_spp_is_numeric_and_matches_table():
    blob = render_repricer_table([item()], date_from=date(2026, 10, 1),
        date_to=date(2026, 10, 6), filters='', cache={})
    with ZipFile(BytesIO(blob)) as archive:
        root = ET.fromstring(archive.read('xl/worksheets/sheet1.xml'))
    ns = {'m': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
    row = root.findall('m:sheetData/m:row', ns)[1]
    cell = row[[c[0] for c in COLUMNS].index('% СПП')]
    assert cell.get('s') == '3'
    assert float(cell.find('m:v', ns).text) == pytest.approx(0.668)
    price = row[[c[0] for c in COLUMNS].index('Цена до СПП')]
    assert float(price.find('m:v', ns).text) == 1426
