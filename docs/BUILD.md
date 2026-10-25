# Збірка Zir SSH

Як зібрати Zir SSH для macOS, Windows і Linux, що виходить на виході і як це встановити.

## Перелік збірок

| Файл | Система | Що це | Як запускати |
|---|---|---|---|
| `zir-macos-arm64.dmg` | macOS, Apple Silicon (M1–M4) | `zir.app` з іконкою | відкрити `.dmg`, перетягнути `zir.app` в «Програми», запускати з Launchpad / Finder |
| `zir-macos-x86_64.dmg` | macOS, Intel | те саме | те саме |
| `zir-macos-arm64`, `zir-macos-x86_64` | macOS | один виконуваний файл без іконки | у терміналі: `./zir-macos-arm64` |
| `zir-windows-x86_64.exe` | Windows 10/11 | один `.exe` з іконкою | подвійний клік або запуск із Windows Terminal |
| `zir-linux-x86_64` | Linux x86_64, glibc ≥ 2.35 | один виконуваний файл | `chmod +x` і запуск у терміналі; `--install-desktop` додає іконку в меню |
| `zir-linux-arm64` | Linux ARM64 (Raspberry Pi 4/5, Graviton) | те саме | те саме |
| `SHA256SUMS` | — | контрольні суми всіх файлів | `shasum -a 256 -c SHA256SUMS` |

На macOS краще брати `.dmg`. Окремий файл `zir-macos-*` при кожному запуску розпаковує себе, і macOS щоразу перевіряє розпаковані бібліотеки, тож старт займає 3–7 секунд. `zir.app` стартує менш ніж за 0.1 секунди.

## Швидкий старт

```sh
make build        # збірка для цього комп'ютера  -> dist/
make build-all    # усі 7 файлів через GitHub Actions -> dist/
make test         # тести
```

На Windows без `make`:

```bat
python scripts\build.py
```

## 1. Локальна збірка (`make build`)

Збирає файли тільки для системи і процесора, на яких запущено команду. PyInstaller не вміє збирати під іншу систему, тому Windows-файл збирається лише на Windows.

**Що потрібно:** Python 3.11+ (3.12 рекомендовано). На macOS додатково Xcode Command Line Tools (`xcode-select --install`), бо потрібні `codesign`, `hdiutil` і `iconutil`.

**Що робить [scripts/build.py](../scripts/build.py):**

1. Створює окреме середовище `.venv-build/` і ставить туди Zir SSH разом із PyInstaller.
2. Збирає один файл `dist/zir-<система>-<процесор>[.exe]` за [packaging/zir.spec](../packaging/zir.spec).
3. Smoke-тест: `--version` і `--report --json --demo` мають відпрацювати без помилок.
4. Тільки на macOS:
   - збирає `zir.app` з іконкою;
   - підписує його ad-hoc підписом (`codesign -s -`), без цього Apple Silicon не запускає код;
   - ще раз проганяє smoke-тест;
   - пакує `.app` разом із посиланням на «Програми» у `dist/zir-macos-<процесор>.dmg`.

Результат на Mac з Apple Silicon:

```
dist/zir-macos-arm64        13.5 MiB
dist/zir-macos-arm64.dmg    14.5 MiB
```

## 2. Усі системи одразу (`make build-all`)

Запускає [.github/workflows/build.yml](../.github/workflows/build.yml). Workflow збирає файли паралельно на рідних машинах GitHub:

| Машина GitHub | Результат |
|---|---|
| `ubuntu-22.04` | `zir-linux-x86_64` |
| `ubuntu-22.04-arm` | `zir-linux-arm64` |
| `macos-14` | `zir-macos-arm64`, `zir-macos-arm64.dmg` |
| `macos-13` | `zir-macos-x86_64`, `zir-macos-x86_64.dmg` |
| `windows-2022` | `zir-windows-x86_64.exe` |

На кожній машині спершу проходять тести, потім збірка зі smoke-тестом. Скрипт дочікується кінця, завантажує всі файли в `dist/` і рахує `SHA256SUMS`.

**Що потрібно:** [GitHub CLI](https://cli.github.com) (`gh auth login`) і репозиторій на GitHub з увімкненими Actions: ваш форк або [LavrinchukAndrii/zir-ssh](https://github.com/LavrinchukAndrii/zir-ssh), якщо у вас є права на запис. Workflow запускається для поточної гілки, тож зміни треба спершу запушити.

Після цього `make build-all` працює з будь-якої системи.

## 3. Реліз

```sh
git tag v0.1.0 && git push origin v0.1.0
```

Тег `v*` запускає ту саму збірку, а потім створює GitHub Release, до якого прикладено всі файли і `SHA256SUMS`.

Чекліст перед тегом:

1. Оновити версію в [zir/\_\_init\_\_.py](../zir/__init__.py) і [pyproject.toml](../pyproject.toml).
2. `make test` зелений.
3. `make build` і ручна перевірка: `./dist/zir-macos-arm64 --demo`, відкрити `.dmg` і запустити `zir.app`.

## Встановлення для користувачів

### macOS

1. Відкрити `zir-macos-arm64.dmg` (Intel: `x86_64`) і перетягнути `zir.app` в «Програми».
2. Перший запуск: файл не підписаний сертифікатом Apple, тому macOS його заблокує. Є два способи обійти:
   - у Finder клацнути правою кнопкою на `zir.app` і вибрати «Відкрити», потім «Відкрити»;
   - або виконати в терміналі `xattr -dr com.apple.quarantine /Applications/zir.app`.
3. Подвійний клік відкриває власне вікно Zir SSH (вбудований термінал), тож у Dock і Cmd+Tab видно саме Zir SSH. Щоб натомість відкривався Terminal або iTerm, задайте змінну середовища `ZIR_TERMINAL=Terminal` (або `ZIR_TERMINAL=iTerm`).
4. Якщо Zir SSH потрібен як команда в терміналі:
   ```sh
   sudo ln -sf /Applications/zir.app/Contents/MacOS/zir /usr/local/bin/zir
   ```
5. `Alt+1..9` — це Option+1..9, додатково нічого вмикати не треба.

### Windows

1. Завантажити `zir-windows-x86_64.exe`. Якщо з'явиться SmartScreen: «Докладніше» → «Виконати все одно».
2. Потрібен вбудований OpenSSH Client: Параметри → Програми → Додаткові компоненти → OpenSSH Client. У Windows 10 1809+ і Windows 11 він зазвичай уже є.
3. Найкраще запускати у Windows Terminal. Подвійний клік відкриває звичайне вікно консолі.
4. Конфіг лежить у `%APPDATA%\zir\hosts.toml`. Під час першого запуску Zir SSH запропонує створити його сам.

### Linux

```sh
chmod +x zir-linux-x86_64
mkdir -p ~/.local/bin && mv zir-linux-x86_64 ~/.local/bin/zir
zir --install-desktop      # іконка і пункт «Zir SSH» у меню застосунків (без sudo)
```

`--install-desktop` записує `~/.local/share/applications/zir.desktop` та іконку в `~/.local/share/icons/hicolor/512x512/apps/`. Пункт меню відкриває Zir SSH у терміналі за замовчуванням. Якщо після цього бінарник переїде в іншу папку, команду треба виконати ще раз.

## Іконка

Іконку малює [packaging/make_icon.py](../packaging/make_icon.py), готові файли лежать у репозиторії:

| Файл | Для чого |
|---|---|
| `packaging/icons/zir.icns` | `zir.app` (macOS) |
| `packaging/icons/zir.ico` | `.exe` (Windows), розміри 16–256 px |
| `packaging/icons/zir.png` | 1024 px, README / сайт |
| `zir/assets/zir.png` | 512 px, вбудована в бінарник для `--install-desktop` |

Перегенерувати після зміни дизайну (`.icns` створюється лише на macOS, бо потрібен `iconutil`):

```sh
.venv/bin/pip install pillow
.venv/bin/python packaging/make_icon.py
```

### Вікно інсталятора (.dmg)

`.dmg` збирає [dmgbuild](https://dmgbuild.readthedocs.io) за налаштуваннями з [packaging/dmg_settings.py](../packaging/dmg_settings.py): вікно 660×440, світлий фон зі стрілкою і підказкою «Перетягніть zir у Applications», іконки 128 px, том має іконку zir. Фон малює [packaging/make_dmg_background.py](../packaging/make_dmg_background.py) (лише macOS, бо потрібен `tiffutil`); якщо змінюєте позиції іконок, міняйте їх в обох файлах:

```sh
.venv/bin/python packaging/make_dmg_background.py
```

Новий Finder (macOS 27) показує власний тулбар навіть тоді, коли `.dmg` просить його сховати, а рядок шляху і статус-бар залежать від налаштувань користувача. Тому весь вміст фону вміщено у верхні ~300 pt вікна, щоб нічого не обрізалось.

## Файли пакування

```
Makefile                      make build / build-all / test / clean
scripts/build.py              уся логіка збірки, smoke-тест, .app/.dmg, GitHub Actions
packaging/entry.py            точка входу для PyInstaller; з .app відкриває власне вікно (zir/window.py)
packaging/zir.spec            onefile або .app-бандл, іконки, Info.plist
packaging/make_icon.py        генератор іконок
packaging/icons/              готові іконки
packaging/dmg_settings.py     вигляд вікна .dmg для dmgbuild: розмір, позиції іконок, фон
packaging/make_dmg_background.py  генератор фону вікна .dmg
packaging/dmg/background.tiff     готовий фон (1x + Retina 2x)
.github/workflows/build.yml   збірка на 5 машинах і реліз за тегом
```

## Тестовий сервер у Docker

Для перевірки веб / PHP / MariaDB / атак і термінала на справжньому Linux у [tests/docker/](../tests/docker/)
є образ: Debian 12, nginx, PHP-FPM 8.2, MariaDB і sshd.

```sh
ssh-keygen -t ed25519 -N "" -f /tmp/zir-testkey
docker build -t zir-testhost tests/docker
docker run -d --name zir-testhost -p 127.0.0.1:2222:22 -p 127.0.0.1:8088:80 \
  -v /tmp/zir-testkey.pub:/root/.ssh/authorized_keys:ro zir-testhost
```

У `hosts.toml`: `target = "root@127.0.0.1"`, `port = 2222`, `identity = "/tmp/zir-testkey"`,
`ssh_options = ["UserKnownHostsFile=/dev/null", "StrictHostKeyChecking=no"]`. Навантаження:
`for i in $(seq 1000); do curl -s http://127.0.0.1:8088/ >/dev/null; done`. Фікстури в
`tests/fixtures/` записані саме з цього сервера.

## Відомі обмеження

- **Немає крос-компіляції.** Кожна система збирається на своїй машині, саме тому існує `make build-all`.
- **Немає підпису Apple або Microsoft.** Звідси попередження Gatekeeper і SmartScreen. Щоб їх прибрати, потрібен Apple Developer ID (99 $/рік) з нотаризацією і сертифікат для Windows.
- **Linux потребує glibc ≥ 2.35** (Ubuntu 22.04+, Debian 12+, RHEL 9+). Для старших систем треба збирати на старішому образі.
- **На Windows немає ControlMaster.** SSH-з'єднання не перевикористовуються, тому опитування трохи повільніше, ніж на macOS і Linux.
- **Вбудований термінал працює на macOS і Linux.** На Windows поки показується команда ssh для запуску окремо (потрібен pywinpty).
- `make clean` видаляє `build/`, `dist/` і `.venv-build/`.
