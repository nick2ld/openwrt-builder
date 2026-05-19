# Local OpenWrt 25.x Firmware Builder

Локальная веб-морда для LXC/VM в Proxmox. Сервис мониторит новые релизы OpenWrt ветки `25.*`, скачивает нужный `openwrt-imagebuilder`, мониторит выбранные APK-репозитории, находит пакеты под архитектуру конкретного роутера, собирает sysupgrade-образы и раздает готовые файлы по HTTP.

На 19 мая 2026 официальный Sysupgrade Server OpenWrt показывает актуальный релиз ветки 25 как `25.12.4`; сервис не зашивает номер релиза в код, а каждый цикл читает `https://downloads.openwrt.org/releases/` и выбирает максимальный релиз с префиксом `25.`.

## 1. Подготовка LXC

Рекомендованный контейнер: Debian 12/13 или Ubuntu 24.04, 2-4 vCPU, 4+ ГБ RAM, 20+ ГБ диска. Для нескольких target/subtarget лучше 40+ ГБ.

В Proxmox LXC включите сеть с доступом в интернет и к локальной сети роутеров. Privileged-контейнер не обязателен, но сборка ImageBuilder должна иметь возможность запускать `make`, `tar`, `zstd` и писать в `/var/lib/openwrt-builder`.

## 2. Установка

В LXC выполните одной командой:

```bash
curl -fsSL https://raw.githubusercontent.com/nick2ld/openwrt-builder/main/install.sh | sudo bash
```

Если нужен нестандартный порт:

```bash
curl -fsSL https://raw.githubusercontent.com/nick2ld/openwrt-builder/main/install.sh | sudo PORT=8090 bash
```

Также можно скопировать каталог в контейнер и запустить локально:

```bash
sudo bash install.sh
```

После установки:

```bash
systemctl status openwrt-builder
journalctl -u openwrt-builder -f
```

Откройте веб-интерфейс:

```text
http://IP_КОНТЕЙНЕРА:8088
```

В веб-интерфейсе можно:

- менять базовые настройки сервера и ветку `25.`
- искать роутеры как в Firmware Selector и автозаполнять `target`, `subtarget`, `profile`, `arch`
- добавлять/удалять роутеры вручную
- задавать список стандартных пакетов ImageBuilder
- добавлять APK-репозитории и списки нужных пакетов
- проверить репозитории кнопкой `Проверить репозитории`
- запустить сборку всех роутеров или одного конкретного
- смотреть статусы заданий, логи и ссылки на готовые firmware

## 3. Как заполнить роутер

Для каждого устройства нужны четыре значения из OpenWrt downloads:

- `target`: например `mediatek`
- `subtarget`: например `filogic`
- `profile`: например `glinet_gl-mt6000`
- `arch`: архитектура APK, например `aarch64_cortex-a53`

Как найти профиль вручную:

```bash
cd /var/lib/openwrt-builder/builders/25.12.x/mediatek-filogic
make info | less
```

Или посмотрите файл `profiles.json` в каталоге target/subtarget на downloads OpenWrt:

```text
https://downloads.openwrt.org/releases/25.12.x/targets/mediatek/filogic/profiles.json
```

В поле `Пакеты ImageBuilder` пишите имена пакетов через пробел или с новой строки:

```text
luci luci-ssl luci-app-attendedsysupgrade owut htop irqbalance
```

## 4. Пользовательские APK

Источник может быть:

- прямая ссылка на `.apk`
- ссылка на HTML-индекс, где лежат `.apk`
- корень репозитория с подпапками версии и архитектуры

В URL можно использовать шаблоны:

```text
{release}  -> актуальный релиз OpenWrt, например 25.12.4
{arch}     -> архитектура роутера, например aarch64_cortex-a53
```

Пример:

```text
https://repo.example.local/openwrt/{release}/{arch}/
```

Если шаблонов нет, сервис сам пробует варианты:

```text
<url>/
<url>/<arch>/
<url>/<release>/
<url>/<release>/<arch>/
```

В поле `Имена пакетов из этого repo` перечислите нужные пакеты:

```text
my-package another-package luci-app-custom
```

Сервис для каждого выбранного роутера берет его `arch`, обходит выбранные репозитории, ищет `.apk` с подходящей архитектурой (`_<arch>.apk`) или универсальные (`_all.apk`), выбирает самый свежий файл по имени версии и скачивает его. Если поле имен пустое, он скачает все подходящие APK из найденного индекса; обычно лучше имена указать явно.

Для HTML-индекса дополнительно можно задать `regex`, например:

```text
my-package_.*\.apk$
```

Сервис копирует найденные APK в ImageBuilder и добавляет путь к ним в `PACKAGES`. Дополнительно при `allow_untrusted_apk=true` он пытается распаковать содержимое APK в `FILES` overlay, чтобы самосборные пакеты без подписи попали в образ даже при строгой проверке подписей.

Важно: untrusted APK - это риск. Используйте только пакеты, которым доверяете, потому что они попадут в прошивку с root-доступом.

## 5. Сборка вручную

В веб-интерфейсе нажмите `Собрать сейчас`. Логи доступны в разделе заданий и в файловой системе:

```bash
ls -lah /var/lib/openwrt-builder/logs
```

Готовые прошивки:

```bash
/var/lib/openwrt-builder/firmware/<router>/<release>/
/var/lib/openwrt-builder/firmware/<router>/latest/
```

HTTP-ссылки:

```text
http://IP_КОНТЕЙНЕРА:8088/firmware/<router>/latest/<имя-sysupgrade-файла>
```

## 6. Address of the sysupgrade server

В OpenWrt установите `luci-app-attendedsysupgrade` и/или `owut` в базовый образ. В поле `Address of the sysupgrade server` можно указать:

```text
http://IP_КОНТЕЙНЕРА:8088
```

Сервис отвечает на `GET /json/v1/overview.json`, `GET /api/overview`, `GET /api/v1/overview` и принимает простые build-запросы на `POST /api/v1/build`. Это легкий локальный prebuilder, а не полная копия upstream ASU с Redis/cache/signing API. Если ваш клиент ASU требует строго совместимое поведение upstream-сервера, используйте прямой URL готового sysupgrade-файла:

```text
http://IP_КОНТЕЙНЕРА:8088/firmware/<router>/latest/<имя-sysupgrade-файла>
```

Практически самый надежный локальный сценарий: сервис собирает файл, роутер скачивает его через `wget`/LuCI/owut, затем выполняется обычный sysupgrade.

## 7. Пример обновления с роутера

```sh
cd /tmp
wget -O fw.bin http://IP_КОНТЕЙНЕРА:8088/firmware/gl-mt6000/latest/openwrt-25.12.x-mediatek-filogic-glinet_gl-mt6000-squashfs-sysupgrade.bin
sha256sum fw.bin
sysupgrade -n fw.bin
```

Для сохранения конфигурации уберите `-n`:

```sh
sysupgrade fw.bin
```

## 8. Обновление сервиса

После правки файлов:

```bash
sudo cp app.py /opt/openwrt-builder/app.py
sudo systemctl restart openwrt-builder
```

## 9. Полезные команды

```bash
journalctl -u openwrt-builder -f
systemctl restart openwrt-builder
du -sh /var/lib/openwrt-builder
find /var/lib/openwrt-builder/firmware -type f
```

## 10. Ограничения

OpenWrt 25.x использует APK. Для самосборных пакетов без подписи штатная установка требует `--allow-untrusted`; ImageBuilder и внешние APK пока ведут себя менее предсказуемо, чем официальный репозиторий. Поэтому сервис делает две вещи: передает APK в `PACKAGES` и распаковывает его payload в overlay. Если пакет имеет сложные зависимости или post-install scripts, лучше поднять полноценный подписанный APK-репозиторий и добавить его в ImageBuilder.
