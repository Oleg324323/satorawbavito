# T2: price enrichment сохраняет source sizes

Этапы 5/8, независимая локальная runtime правка после `4c3f6fa`.

Два helper-а (`repricer_bff._merge_good_spp_fields` и
`repricer_sync._apply_external_spp_prices_to_goods`) заменяли `sizes` на массив
из одной строки при enrichment. Все последующие варианты товара терялись.
Теперь первая enriched строка сохраняет прежний результат, а хвост исходного
списка остаётся целиком: ID, цены, techSizeName и неизвестные source fields.
Buyer/Club значения на другие размеры не копируются. Source enrichment payload
не мутируется; существующие tail objects сохраняют прежние ссылки.

Это не решает неопределённый nm-only buyer provenance и first-size mapping в
legacy flow. Никакой current buyer source не объявлен подтверждённым. Canonical
offer mapping, source manifest/current pointer/daily persistence остаются отдельными
gates. Формулы, flags, calls, schema и consumers этой правкой не переключаются.

TDD RED: 2 failed / 7 passed, exit1 — оба failures доказали удаление sizes.
GREEN scoped size/unit/calculation/price-guard group: 130 passed, exit0.
Независимый critic: 9 passed, exit0, существенных blockers нет; aliasing ограничения
описаны выше. Дополнительно compileall/diff-check перед commit.

Команда из backend, local wave1-integration Python:

```sh
python -m pytest -q -p tests.repricer_offline_plugin \
  tests/test_wb_price_size_preservation.py tests/test_wb_price_units.py \
  tests/test_repricer_calculation_characterization.py tests/test_wb_repricing_price_inputs.py
```

Никаких provider/production/DB/Redis действий. This is source-row preservation,
not canonical storage completion or real-price activation.
