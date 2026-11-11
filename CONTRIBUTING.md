# Разработка

Нужны Python 3.12+ и Node.js 24. Тесты используют локальные подмены границ HA;
рабочие Telegram, Google, ChatGPT и Home Assistant для них не нужны.

```sh
python -m pip install -r requirements-test.txt
python -m unittest discover -s tests
node --test tests/test_panel.cjs
ruff check custom_components tests tools
ruff format --check custom_components tests tools
python tools/check_release.py
python tools/build_release.py
```

Меняйте версию одновременно в `manifest.json`, `const.py` и `CHANGELOG.md`.
Публикация основной ветки проходит через проверки и создаёт GitHub release с этой
версией, архивом и SHA-256. Повторный запуск не заменяет существующий релиз.
Не включайте пользовательские данные и исполняемый файл Codex в исходники или архив.
Сохраняйте совместимость домена, unique IDs, хранилища и связей календаря при обновлениях.

Разработка исходников может происходить в отдельном checkout. Рабочей установкой
после перехода на Marketplace управляет сам Marketplace; `tools/install.py` без
`--runtime-only` предназначен только для локальной разработки.
