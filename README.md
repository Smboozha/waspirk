# Sarpik — VPN-рычажок для Linux

Sarpik by smboozha · t.me/smboozha · v2.0

Десктопная версия Nova для Linux: встроенные WARP-узлы (AmneziaWG), узлы
`vless://` с транспортами WS/gRPC/XHTTP/HTTP Upgrade/REALITY (Xray-core), свои
конфиги, автофейловер — при отказе узла приложение само переключается на
следующий, как в мобильном Nova.

## Что умеет

- Включение/выключение VPN рычажком (клик, ↑/↓, пробел).
- Встроенные WARP-узлы: при первом запуске подхватываются конфиги из
  `~/Downloads/confs/*.conf` (t1, t2 и т.д.) — помечаются как «встроенные».
- Импорт своих конфигов AmneziaWG кнопкой «Импорт».
- Узлы VLESS: кнопка `vless://` — вставить ссылку из подписки. Поддерживаются
  type=tcp/ws/grpc/httpupgrade/xhttp/kcp и security=reality/tls/none.
  Движок — вендоренный форк Xray-core 26.7.28, собранный в
  `engines/sarpik-xray` (TUN-интерфейс, как в мобильном Nova).
- Автофейловер: при недоступности узла пробует остальные по очереди, включая
  переходы между AWG и VLESS.
- Мониторинг соединения: если канал пропал — автоматическое переключение.
- Выбор стартового узла из списка.

## Запуск

```bash
./run.sh
```

## Требования

- `amneziawg-dkms` и `amneziawg-tools` (уже стоят на CachyOS)
- python3 + python-gobject + pycairo
- собранный движок `engines/sarpik-xray` (бинарник уже в комплекте; исходники —
  в `engines/xray-src`, пересборка: `cd engines/xray-src && go build -ldflags=-checklinkname=0 -o ../sarpik-xray .`)

## Один раз: sudo без пароля

Приложение поднимает/опускает туннели через `sudo` и меняет DNS через
`resolvectl`. Разрешение настраивается файлом `/etc/sudoers.d/sarpik`:

```bash
echo "$USER ALL=(ALL) NOPASSWD: /usr/bin/awg-quick up *, /usr/bin/awg-quick down *, /usr/bin/kill *, /usr/bin/pkill *, /usr/bin/resolvectl dns *, /usr/bin/resolvectl domain *, /usr/bin/resolvectl revert *, /usr/bin/resolvectl flush-caches, /home/$USER/progects/sarpik/Sarpik/engines/sarpik-xray *" | sudo tee /etc/sudoers.d/sarpik
```

## Куда кладутся узлы

`~/.config/sarpik/profiles/` — `*.conf` (AWG) и `*.vless` (ссылка на узел).
`.meta`-файлы рядом отмечают встроенные узлы.

## Примечание о модуле ядра

При обновлении ядра CachyOS пересобери модуль amneziawg:

```bash
sudo dkms autoinstall -k $(uname -r)
```

Если сборка падает из-за смены API ядра (`setup_udp_tunnel_sock`), в
`/usr/src/amneziawg-1.0.0/socket.c` замените передаваемые значения с
`socket *` на `->sk` и повторите `dkms remove amneziawg/1.0.0 --all &&
dkms add /usr/src/amneziawg-1.0.0 && dkms autoinstall -k $(uname -r)`.
