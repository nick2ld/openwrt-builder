# OpenWrt Custom Local Builder

OpenWrt Custom Local Builder is a local web application for a Proxmox LXC/VM. It watches the OpenWrt 25.x release branch, downloads the correct ImageBuilder for your selected routers, waits for required external APK packages to appear, builds custom sysupgrade images, keeps the latest firmware files available over HTTP, and can be used as a local sysupgrade server.

OpenWrt Custom Local Builder - локальное веб-приложение для LXC/VM в Proxmox. Оно следит за релизами OpenWrt 25.x, скачивает нужный ImageBuilder под выбранные роутеры, ждет появления внешних APK, собирает кастомные sysupgrade-прошивки, хранит последние готовые файлы и раздает их по HTTP для обновления роутеров.

[Русский](#русский) | [English](#english)

## Русский

### Что делает приложение

- Мониторит актуальный релиз OpenWrt по префиксу ветки, например `25.`.
- Ищет роутеры как OpenWrt Firmware Selector и заполняет `target`, `subtarget`, `profile`, `arch` и штатный список пакетов.
- Для каждого роутера хранит индивидуальный список пакетов ImageBuilder.
- Мониторит внешние APK-источники: прямые `.apk`, HTML-индексы, repo-каталоги и GitHub Releases.
- Подбирает APK под нужные `release`, `arch`, `target/subtarget` и имя пакета.
- Если для нового релиза не хватает обязательных APK, сборка не запускается и переходит в ожидание APK.
- Когда все APK найдены, автоматически собирает прошивку.
- Держит последние 3 успешные прошивки на каждый роутер; старые, неудачные и отмененные задания чистятся.
- Показывает статус каждого роутера, progress bar задания, последнюю строку лога и live-лог в модальном окне.
- Поддерживает русский и английский интерфейс. Язык хранится на уровне браузера пользователя.

### Требования

Рекомендуемый контейнер: Debian 12/13 или Ubuntu 24.04, 2-4 vCPU, 4+ ГБ RAM, 20+ ГБ диска. Для нескольких target/subtarget лучше 40+ ГБ.

Контейнеру нужен доступ в интернет и в локальную сеть роутеров. Privileged LXC не обязателен, но ImageBuilder должен запускать `make`, `tar`, `zstd` и писать в `/var/lib/openwrt-builder`.

### Установка и обновление

В LXC выполните:

```bash
curl -fsSL https://raw.githubusercontent.com/nick2ld/openwrt-builder/main/install.sh | sudo bash
```

Нестандартный порт:

```bash
curl -fsSL https://raw.githubusercontent.com/nick2ld/openwrt-builder/main/install.sh | sudo PORT=8090 bash
```

Повторный запуск той же команды обновляет приложение до актуальной версии из `main`: установщик скачивает свежие файлы, останавливает сервис, сохраняет `/var/lib/openwrt-builder/config.json` и данные, делает backup старого `/opt/openwrt-builder`, устанавливает новую версию и запускает сервис.

После установки:

```bash
systemctl status openwrt-builder
journalctl -u openwrt-builder -f
```

Откройте:

```text
http://IP_КОНТЕЙНЕРА:8088
```

Кнопка обновления в веб-интерфейсе использует root-helper через `sudo` и `systemd-run`. Unit устанавливается с `NoNewPrivileges=false`, иначе LXC/Debian может блокировать `sudo`.

### Язык интерфейса

В шапке есть переключатель `RU / EN`. Выбранный язык сохраняется в `localStorage` браузера, поэтому у разных пользователей может быть разный язык.

Файлы локализации лежат отдельно от скрипта:

```text
locales/ru.json
locales/en.json
```

Новые языки не добавляются заранее. Их имеет смысл добавлять только после предложений пользователей.

### Как добавить роутер

1. Откройте веб-интерфейс.
2. В блоке `Роутеры` нажмите `Добавить роутер`.
3. Введите модель, например `Cudy WR3000`, `GL-MT6000` или `Archer C7`.
4. Нажмите `Заполнить поля` у найденной модели.
5. Проверьте список пакетов и нажмите `Сохранить роутер`.

Приложение заполнит `target`, `subtarget`, `profile`, `arch` и базовые пакеты из OpenWrt `profiles.json`.

Ручной режим нужен, если устройство не находится поиском. Тогда укажите:

```text
target: mediatek
subtarget: filogic
profile: cudy_wr3000-v1
arch: aarch64_cortex-a53
```

Список пакетов можно писать через пробел или с новой строки:

```text
luci luci-app-attendedsysupgrade luci-mod-dashboard https-dns-proxy
```

### Внешние APK / репозитории

Источник может быть:

- прямая ссылка на `.apk`;
- HTML-индекс с `.apk`;
- корень repo с подпапками версии и архитектуры;
- GitHub Releases, например `https://github.com/Slava-Shchipunov/awg-openwrt/releases`.

В URL поддерживаются шаблоны:

```text
{release}  -> текущий релиз OpenWrt, например 25.12.4
{arch}     -> архитектура роутера, например aarch64_cortex-a53
```

Если шаблонов нет, сервис сам пробует:

```text
<url>/
<url>/<arch>/
<url>/<release>/
<url>/<release>/<arch>/
```

В поле `Имена пакетов из этого repo` укажите обязательные пакеты:

```text
amneziawg-tools kmod-amneziawg luci-i18n-amneziawg-ru luci-proto-amneziawg
```

Для `Slava-Shchipunov/awg-openwrt` пример источника:

```text
Название: amneziawg
URL: https://github.com/Slava-Shchipunov/awg-openwrt/releases
Arch фильтр: можно оставить пустым
Имена пакетов:
amneziawg-tools kmod-amneziawg luci-i18n-amneziawg-ru luci-proto-amneziawg
```

Для `mediatek/filogic` и `aarch64_cortex-a53` приложение будет искать файлы вида:

```text
amneziawg-tools_v25.12.4_aarch64_cortex-a53_mediatek_filogic.apk
kmod-amneziawg_v25.12.4_aarch64_cortex-a53_mediatek_filogic.apk
luci-i18n-amneziawg-ru_v25.12.4_aarch64_cortex-a53_mediatek_filogic.apk
luci-proto-amneziawg_v25.12.4_aarch64_cortex-a53_mediatek_filogic.apk
```

Если обязательный APK для нового релиза еще не опубликован, статус роутера будет `Нет необходимых APK`, а задание получит статус `waiting_apks`. Планировщик продолжит проверять репозитории по таймеру и запустит сборку автоматически, когда все пакеты появятся.

`allow_untrusted_apk=true` добавляет для ImageBuilder режим установки неподписанных APK. Это удобно для своих пакетов, но небезопасно для неизвестных источников: такие пакеты попадут в прошивку с root-доступом.

### Сборка

Кнопка `Собрать все доступные прошивки` запускает сборку всех включенных роутеров, для которых есть новый релиз и полный набор обязательных APK. Она не должна принудительно пересобирать уже готовую актуальную прошивку.

В таблице роутеров есть отдельная кнопка `Собрать` для конкретного роутера. Она запускает ручную сборку выбранного роутера.

Логи доступны в UI и на диске:

```bash
ls -lah /var/lib/openwrt-builder/logs
```

Готовые прошивки:

```text
/var/lib/openwrt-builder/firmware/<router>/<release>/
/var/lib/openwrt-builder/firmware/<router>/latest/
```

HTTP:

```text
http://IP_КОНТЕЙНЕРА:8088/firmware/<router>/latest/<sysupgrade-file>
```

### Address of the sysupgrade server

В OpenWrt установите `luci-app-attendedsysupgrade` и укажите корень сервера:

```text
http://IP_КОНТЕЙНЕРА:8088
```

Не добавляйте `/api/asu`, если клиент ожидает обычный ASU-сервер. Совместимые endpoints доступны от корня:

```text
http://IP_КОНТЕЙНЕРА:8088/json/v1/overview.json
http://IP_КОНТЕЙНЕРА:8088/json/v1/latest.json
http://IP_КОНТЕЙНЕРА:8088/api/v1/build
```

Для старых сохраненных настроек сервис также отвечает на `/api/asu/...`.

Это локальный prebuilder, а не полная копия upstream ASU. Самый надежный сценарий: приложение заранее собирает прошивку, роутер получает ссылку на готовый sysupgrade-файл и обновляется обычным `sysupgrade`.

### Обновление с роутера вручную

```sh
cd /tmp
wget -O fw.bin http://IP_КОНТЕЙНЕРА:8088/firmware/cudy-wr3000-v1/latest/openwrt-25.12.x-mediatek-filogic-cudy_wr3000-v1-squashfs-sysupgrade.bin
sha256sum fw.bin
sysupgrade fw.bin
```

Для чистой установки без сохранения настроек:

```sh
sysupgrade -n fw.bin
```

### Полезные команды

```bash
journalctl -u openwrt-builder -f
systemctl restart openwrt-builder
du -sh /var/lib/openwrt-builder
find /var/lib/openwrt-builder/firmware -type f
```

## English

### What it does

- Watches the current OpenWrt release by branch prefix, for example `25.`.
- Searches routers like OpenWrt Firmware Selector and fills `target`, `subtarget`, `profile`, `arch`, and default packages.
- Stores a separate ImageBuilder package list for every router.
- Monitors external APK sources: direct `.apk` links, HTML indexes, repo directories, and GitHub Releases.
- Matches APK files by `release`, `arch`, `target/subtarget`, and package name.
- If a new OpenWrt release is available but required APK files are missing, the build is not started and waits for APKs.
- Builds automatically once all required APK files are available.
- Keeps the latest 3 successful firmware builds per router; older, failed, and cancelled jobs are cleaned up.
- Shows per-router status, job progress, the last log line, and a live log modal.
- Supports Russian and English UI. The selected language is stored per browser user.

### Requirements

Recommended container: Debian 12/13 or Ubuntu 24.04, 2-4 vCPU, 4+ GB RAM, 20+ GB disk. Use 40+ GB if you build several target/subtarget combinations.

The container needs internet access and access to your router LAN. A privileged LXC is not required, but ImageBuilder must be able to run `make`, `tar`, `zstd`, and write to `/var/lib/openwrt-builder`.

### Install and update

Run in the LXC:

```bash
curl -fsSL https://raw.githubusercontent.com/nick2ld/openwrt-builder/main/install.sh | sudo bash
```

Custom port:

```bash
curl -fsSL https://raw.githubusercontent.com/nick2ld/openwrt-builder/main/install.sh | sudo PORT=8090 bash
```

Running the same command again updates the application from `main`: the installer downloads fresh files, stops the service, keeps `/var/lib/openwrt-builder/config.json` and data, backs up the old `/opt/openwrt-builder`, installs the new version, and starts the service again.

After installation:

```bash
systemctl status openwrt-builder
journalctl -u openwrt-builder -f
```

Open:

```text
http://CONTAINER_IP:8088
```

The web UI update button uses a root helper via `sudo` and `systemd-run`. The service unit is installed with `NoNewPrivileges=false`; otherwise some LXC/Debian setups block `sudo`.

### UI language

The header has an `RU / EN` selector. The selected language is saved in browser `localStorage`, so different users can use different languages.

Locale files are separate from the Python script:

```text
locales/ru.json
locales/en.json
```

Additional languages are intentionally not bundled until users propose them.

### Add a router

1. Open the web UI.
2. In `Routers`, click `Add router`.
3. Search for a model, for example `Cudy WR3000`, `GL-MT6000`, or `Archer C7`.
4. Click `Fill fields` on the selected model.
5. Review the package list and click `Save router`.

The application fills `target`, `subtarget`, `profile`, `arch`, and default packages from OpenWrt `profiles.json`.

Manual mode is only needed when search does not find the device. Example:

```text
target: mediatek
subtarget: filogic
profile: cudy_wr3000-v1
arch: aarch64_cortex-a53
```

Package lists can be space-separated or line-separated:

```text
luci luci-app-attendedsysupgrade luci-mod-dashboard https-dns-proxy
```

### External APK / repositories

A source can be:

- a direct `.apk` URL;
- an HTML index with `.apk` links;
- a repo root with release and arch subdirectories;
- GitHub Releases, for example `https://github.com/Slava-Shchipunov/awg-openwrt/releases`.

URL templates:

```text
{release}  -> current OpenWrt release, for example 25.12.4
{arch}     -> router architecture, for example aarch64_cortex-a53
```

If no templates are used, the service tries:

```text
<url>/
<url>/<arch>/
<url>/<release>/
<url>/<release>/<arch>/
```

List required package names in `Package names from this repo`:

```text
amneziawg-tools kmod-amneziawg luci-i18n-amneziawg-ru luci-proto-amneziawg
```

Example for `Slava-Shchipunov/awg-openwrt`:

```text
Name: amneziawg
URL: https://github.com/Slava-Shchipunov/awg-openwrt/releases
Arch filter: leave empty unless you want to restrict it
Package names:
amneziawg-tools kmod-amneziawg luci-i18n-amneziawg-ru luci-proto-amneziawg
```

For `mediatek/filogic` and `aarch64_cortex-a53`, it will look for files like:

```text
amneziawg-tools_v25.12.4_aarch64_cortex-a53_mediatek_filogic.apk
kmod-amneziawg_v25.12.4_aarch64_cortex-a53_mediatek_filogic.apk
luci-i18n-amneziawg-ru_v25.12.4_aarch64_cortex-a53_mediatek_filogic.apk
luci-proto-amneziawg_v25.12.4_aarch64_cortex-a53_mediatek_filogic.apk
```

If a required APK for the new release is not published yet, the router status becomes `Required APK missing`, and the job status becomes `waiting_apks`. The scheduler keeps checking sources and starts the build automatically once every required file is available.

`allow_untrusted_apk=true` enables untrusted APK installation in ImageBuilder. This is useful for your own packages, but unsafe for unknown sources: those packages become part of the firmware with root privileges.

### Build

`Build all available firmware` starts builds for all enabled routers that have a new release and a complete required APK set. It should not force-rebuild firmware that is already current.

The router table also has a per-router `Build` button for manual builds.

Logs are available in the UI and on disk:

```bash
ls -lah /var/lib/openwrt-builder/logs
```

Firmware files:

```text
/var/lib/openwrt-builder/firmware/<router>/<release>/
/var/lib/openwrt-builder/firmware/<router>/latest/
```

HTTP:

```text
http://CONTAINER_IP:8088/firmware/<router>/latest/<sysupgrade-file>
```

### Address of the sysupgrade server

Install `luci-app-attendedsysupgrade` on OpenWrt and set the server root:

```text
http://CONTAINER_IP:8088
```

Do not add `/api/asu` unless your client is already configured that way. Compatible endpoints are served from the root:

```text
http://CONTAINER_IP:8088/json/v1/overview.json
http://CONTAINER_IP:8088/json/v1/latest.json
http://CONTAINER_IP:8088/api/v1/build
```

The service also responds under `/api/asu/...` for older saved settings.

This is a local prebuilder, not a full upstream ASU clone. The most reliable workflow is: the application prebuilds firmware, the router gets the ready sysupgrade file URL, and OpenWrt performs a normal `sysupgrade`.

### Manual router upgrade

```sh
cd /tmp
wget -O fw.bin http://CONTAINER_IP:8088/firmware/cudy-wr3000-v1/latest/openwrt-25.12.x-mediatek-filogic-cudy_wr3000-v1-squashfs-sysupgrade.bin
sha256sum fw.bin
sysupgrade fw.bin
```

Clean upgrade without preserving settings:

```sh
sysupgrade -n fw.bin
```

### Useful commands

```bash
journalctl -u openwrt-builder -f
systemctl restart openwrt-builder
du -sh /var/lib/openwrt-builder
find /var/lib/openwrt-builder/firmware -type f
```
