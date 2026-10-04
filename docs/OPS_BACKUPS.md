# Резервные копии и восстановление Huddle

## Ежедневная копия

Скрипт `tools/backup_huddle.py` сохраняет данные комнат и `registry.json` в
`~/.mcp-huddle/backups/automatic/`. Он включает `meta.json`, `status.json`,
`messages.jsonl`, `notify_registry.json`, журналы событий агентов и их файлы
последнего ответа. JSON-файлы комнат и `messages.jsonl` копируются по одному
под теми же блокировками, что использует Huddle. Журналы и файлы последнего
ответа не блокируются всеми агентами, поэтому их последняя строка может
скопироваться неполной. Весь снимок также не является одной общей точкой
времени.

Каждый снимок содержит `manifest.json` с размером и SHA-256 контрольной суммой
каждого сохранённого файла. SHA-256 — способ проверить, что байты не изменились.
Скрипт сначала записывает временный снимок, проверяет его и только затем
публикует. После успешной проверки старые автоматические снимки удаляются, если
им больше 21 дня и после удаления останется не меньше двух проверенных копий.
Неизвестные файлы и символические ссылки скрипт не удаляет. Каталог и файлы
снимка доступны только владельцу учётной записи.

Обычный запуск:

```bash
"/Users/kolotovalexander/Apps Projects/AgentSync/mcp/huddle/.venv/bin/python" \
  "/Users/kolotovalexander/Apps Projects/AgentSync/mcp/huddle/tools/backup_huddle.py" \
  --home "$HOME/.mcp-huddle" \
  --destination "$HOME/.mcp-huddle/backups/automatic" \
  --keep-days 21
```

Для проверки плана без записи добавьте `--dry-run`. Чтобы проверить уже созданный
снимок, выполните:

```bash
"/Users/kolotovalexander/Apps Projects/AgentSync/mcp/huddle/.venv/bin/python" \
  "/Users/kolotovalexander/Apps Projects/AgentSync/mcp/huddle/tools/backup_huddle.py" \
  --verify "$HOME/.mcp-huddle/backups/automatic/snapshot-..."
```

Планировщик macOS `launchd` запускает эту команду каждый день в 03:30 по местному
времени и при загрузке учётной записи. Задание называется
`com.kolotovalexander.huddle.backup`. Сервер dashboard использует задание
`com.kolotovalexander.huddle.dashboard`; оно и bridge MCP работают отдельно от
резервного копирования.

## Восстановление

Восстановление заменяет файлы Huddle. Сначала остановите оба сервера: dashboard
на порту `8014` и bridge MCP на порту `45111`. Пока они работают, они могут
перезаписать восстановленные данные. Убедитесь, что выбрана проверенная копия:

```bash
"/Users/kolotovalexander/Apps Projects/AgentSync/mcp/huddle/.venv/bin/python" \
  "/Users/kolotovalexander/Apps Projects/AgentSync/mcp/huddle/tools/backup_huddle.py" \
  --verify "$HOME/.mcp-huddle/backups/automatic/snapshot-..."
```

Перед восстановлением сохраните текущую папку `rooms` и `registry.json` в другое
место. Затем скопируйте из выбранного снимка папки `rooms` и файл `registry.json`
обратно в `~/.mcp-huddle/`. Если в снимке нет `registry.json`, сохраните текущий
файл. После запуска серверов проверьте нужные комнаты через dashboard или
`room_list`.

Снимок не включает `delivery.json`, системные журналы и значения переменных
окружения с ключами доступа. Он восстанавливает историю комнат и профильный
`registry.json`, но не все настройки установки. В метаданных комнаты могут остаться
устаревшие PID (идентификаторы процессов) и занятые leases (записи о занятом
агенте) от старого запуска. Не считайте старое состояние процесса живым:
проверьте комнату, при необходимости используйте `room_reclaim` для своей
открытой комнаты или закройте её штатным инструментом. Восстановление не
возобновляет работу агентов и не включает серверы.
