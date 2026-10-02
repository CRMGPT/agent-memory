# Проверка agent-memory на macOS

На macOS авторы плагин еще не запускали, так что это первый настоящий прогон. Всего около 15
минут: 5 на подготовку, 5 на самопроверку, 5 на короткий живой сценарий. Ничего не отправляется
автоматически; в конце вы присылаете один текстовый файл.

## 1. Проверить, что все нужное есть

В Терминале:

```bash
python3 --version   # нужна 3.10 или новее
git --version
claude --version
```

Если `python3` версии 3.9 (это Python из Command Line Tools), поставьте новее через
[Homebrew](https://brew.sh): `brew install python`, откройте новое окно Терминала и проверьте еще раз.
Без Python 3.10 или новее плагин остается выключенным и пишет об этом при старте сессии Claude.
Без Command Line Tools файлы `/usr/bin/python3` и `/usr/bin/git` - только заглушки, которые при запуске
открывают окно установки: сначала поставьте Python 3.10+ (python.org или Homebrew), а для git -
Command Line Tools (`xcode-select --install`). Хук плагина заглушку `python3` не запускает.

Если на этом Mac еще не делали `git commit`, задайте имя для тестовых коммитов:
`git config --global user.name "test"` и `git config --global user.email "test@example.invalid"`.

## 2. Установить плагин

Запустите Claude Code в любой папке (`claude`) и введите:

```
/plugin marketplace add CRMGPT/agent-memory
/plugin install agent-memory@agent-memory
```

Если плагин пришел архивом zip, распакуйте его и укажите папку:
`/plugin marketplace add /путь/к/agent-memory`. Затем выйдите из Claude Code (`/exit`).

## 3. Самопроверка (тесты и проверки в один файл)

```bash
L=$(find ~/.claude/plugins -name launcher.py -path "*agent-memory*" | head -1); echo "$L"
python3 -m venv /tmp/am-venv && /tmp/am-venv/bin/python -m pip install --quiet pytest
/tmp/am-venv/bin/python "$L" cli selftest ~/Desktop/agent-memory-diagnostics.txt
```

Первая строка должна напечатать путь, который кончается на `bin/launcher.py`. Самопроверка
прогоняет тесты плагина (несколько минут) и его проверки и пишет файл
`~/Desktop/agent-memory-diagnostics.txt`. В нем версии, итоги тестов и проверок. Домашняя папка,
имя пользователя, имя компьютера и временные папки заменены заглушками; содержимого памяти и
ваших запросов там нет. Пожалуйста, прочитайте файл перед отправкой.

## 4. Живой сценарий (два проекта, около 5 минут)

Создайте два маленьких проекта:

```bash
for p in /tmp/am-a /tmp/am-b; do
  mkdir -p "$p" && cd "$p" && git init -q
  printf 'def apply_discount(price, percent):\n    return price * (100 - percent) / 100\n' > prices.py
  git add -A && git commit -qm start
done
```

1. `cd /tmp/am-a && claude`, отправьте:
   `apply_discount must round money to 2 decimals with round-half-up (2.675 becomes 2.68, not banker's rounding). Implement it with Decimal and add a test.`
   Дождитесь, пока Claude полностью закончит задачу, затем `/exit`.
2. Снова `cd /tmp/am-a && claude` (новая сессия), отправьте:
   `Add apply_tax(price, rate_percent) to prices.py, consistent with how this project rounds money.`
   Ожидается: Claude сразу использует округление half-up. Затем `/exit`.
3. `cd /tmp/am-b && claude`, отправьте тот же запрос, что в шаге 2.
   Ожидается: про правило half-up Claude ничего не знает (у проекта B памяти нет). Затем `/exit`.

Просить Claude что-то запомнить не нужно.

## 5. Что прислать

- файл `~/Desktop/agent-memory-diagnostics.txt` (после того как прочитаете);
- по живому сценарию: знала ли новая сессия в A правило half-up без подсказки (да/нет), знала ли
  его сессия в B (да/нет), и любое сообщение про agent-memory, которое Claude Code показал при старте
  сессии (скопируйте текст).

Сами папки памяти присылать не нужно.

## 6. Удалить и убрать за собой

В Claude Code:

```
/plugin uninstall agent-memory@agent-memory
/plugin marketplace remove agent-memory
```

В Терминале:

```bash
rm -rf /tmp/am-a /tmp/am-b /tmp/am-venv "$HOME/Library/Application Support/agent-memory"
```
