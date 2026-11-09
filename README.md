# botpushfiles

Bot Telegram: gửi link hoặc file cho bot, bot tự upload lên Telegram.

## Cài đặt

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # rồi điền BOT_TOKEN, TARGET_CHAT_ID, ALLOWED_USER_IDS
.venv/bin/python bot.py
```

## Cách dùng

- Dán 1 hoặc nhiều link vào chat (hoặc `/up <link>`): bot tải file về rồi upload.
- Link video (TikTok, YouTube, file .mp4...): bot hiện nút chọn 🎬 MP4 / 📱 MP4 360p / 🎵 Audio gốc.
- Link chỉ có âm thanh (SoundCloud, file .mp3...): bot tải luôn, không hỏi. Âm thanh luôn giữ nguyên chất lượng gốc (không mã hoá lại).
- Gửi file/ảnh/video/audio: bot copy sang `TARGET_CHAT_ID` (không giới hạn dung lượng).
- `/start`: xem hướng dẫn và user ID của bạn.

## Giới hạn

Bot API chính thức chỉ cho upload tối đa 50 MB. Muốn tới 2000 MB, chạy
[telegram-bot-api](https://github.com/tdlib/telegram-bot-api) local:

```bash
sudo apt install -y cmake gperf g++ libssl-dev zlib1g-dev
git clone --recursive https://github.com/tdlib/telegram-bot-api.git telegram-bot-api/src
cmake -S telegram-bot-api/src -B telegram-bot-api/build -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=$PWD/telegram-bot-api
cmake --build telegram-bot-api/build --target install -j4
```

Điền `TELEGRAM_API_ID`, `TELEGRAM_API_HASH` (từ https://my.telegram.org) vào `.env`, gọi
`https://api.telegram.org/bot<TOKEN>/logOut` một lần, chạy `./run_bot_api.sh`, rồi đặt
`LOCAL_BOT_API_URL=http://127.0.0.1:8081` và khởi động lại bot.

## Tiến trình upload

Khi `.env` có `TELEGRAM_API_ID` và `TELEGRAM_API_HASH`, bot upload qua MTProto (đăng nhập bằng chính bot token,
lưu phiên ở `mtproto.session`). Telegram xác nhận từng phần 512 KB nên bot hiện được % upload thật, và file tới
2000 MB đi được mà không cần Local Bot API server. Đặt `MTPROTO_UPLOAD=0` để quay về upload qua Bot API
(chỉ hiện thời gian đã upload).

## Phát triển

```bash
.venv/bin/pip install -r requirements-dev.txt   # thêm pytest, ruff
.venv/bin/ruff check .                           # kiểm tra code
.venv/bin/python -m pytest -q                    # chạy test (không cần mạng, không dùng .env thật)
systemctl --user restart botpushfiles            # áp dụng code mới cho bot đang chạy
```

Quy trình sửa code:

1. Tạo nhánh mới: `git switch -c ten-thay-doi`.
2. Sửa code, thêm test cho phần mới trong `tests/`.
3. Chạy `ruff check .` và `pytest`. Cả hai phải qua.
4. Commit, push, mở Pull Request. GitHub Actions ([.github/workflows/ci.yml](.github/workflows/ci.yml)) tự chạy lint,
   test, ShellCheck và build Docker.
5. CI xanh thì merge vào `main`, rồi cập nhật máy chạy bot: `git pull` → `systemctl --user restart botpushfiles`
   (Docker: `sudo docker compose up -d --build`).

## Bảo mật

- `.env`, `*.session` và `dist/` chứa token hoặc phiên đăng nhập của bot. Không commit, không gửi cho ai.
- Luôn đặt `ALLOWED_USER_IDS`. Nếu để trống, ai tìm thấy bot cũng dùng được, và có thể bắt máy chạy bot tải
  bất kỳ địa chỉ nào, kể cả địa chỉ trong mạng nội bộ.
- Lộ token: @BotFather → `/revoke`, rồi xoá `mtproto.session` và cập nhật `.env`.

## Tự chạy khi bật máy

```bash
./install-autostart.sh                       # cài service systemd, bot tự bật và tự chạy lại khi lỗi
systemctl --user status botpushfiles         # xem trạng thái
systemctl --user restart botpushfiles        # khởi động lại
systemctl --user disable --now botpushfiles  # tắt hẳn
```

## Chuyển bot sang máy khác qua GitHub

`.env` (chứa token) và `*.session` nằm trong `.gitignore` nên không được đẩy lên GitHub.
Sang máy mới phải tạo lại `.env`. Không đưa token lên GitHub, kể cả repo private.

### 1. Đẩy code lên GitHub (máy cũ)

Tạo repo **Private** trên GitHub, rồi chạy:

```bash
git init
git add .
git status          # kiểm tra: không được có .env, mtproto.session
git commit -m "botpushfiles"
git branch -M main
git remote add origin https://github.com/<tên-bạn>/botpushfiles.git
git push -u origin main
```

### 2. Máy cũ tự nhường bot

Một token chỉ chạy được ở 1 máy tại 1 thời điểm. **Máy nào bật bot sau cùng thì giữ bot**:

- Khi bot ở máy mới chạy, Telegram báo `Conflict` cho máy cũ. Bot ở máy cũ ghi log
  `Another machine started this bot; stopping here` rồi tự thoát.
- Bot ở máy cũ không tự bật lại cho tới lần khởi động máy sau. Khi đó máy cũ lại thành máy mới nhất và giành lại bot.
- Trong 60 giây đầu sau khi khởi động, bot không nhường máy. Nhờ vậy máy vừa bật luôn giữ được bot.
- Muốn lấy lại bot ngay mà không cần khởi động lại máy: `systemctl --user start botpushfiles`
  (Docker: `sudo docker compose up -d`).

Muốn máy cũ không bao giờ chạy bot nữa: `systemctl --user disable --now botpushfiles`.

> Máy đã cài service từ trước phải chạy lại `./install-autostart.sh` một lần. Bản cũ dùng `Restart=always`,
> nên bot đã nhường máy vẫn tự bật lại và giành lại bot.

### 3. Chạy ở máy mới

Clone repo và tạo `.env`:

```bash
git clone https://github.com/<tên-bạn>/botpushfiles.git
cd botpushfiles
cp .env.example .env
nano .env   # điền BOT_TOKEN, TARGET_CHAT_ID, ALLOWED_USER_IDS, TELEGRAM_API_ID, TELEGRAM_API_HASH
```

Lấy giá trị từ `.env` ở máy cũ, chuyển qua kênh riêng (không gửi công khai).

**Cách A: Docker (nên dùng).** Có sẵn local Bot API server, không phải build:

```bash
./setup-vps.sh                     # cài Docker và chạy bot, tự chạy lại khi khởi động máy
sudo docker compose logs -f bot    # xem log
```

**Cách B: không dùng Docker.** Để trống `LOCAL_BOT_API_URL` trong `.env`:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
./install-autostart.sh
```

File tới 2000 MB vẫn gửi được nhờ upload MTProto (cần `TELEGRAM_API_ID` và `TELEGRAM_API_HASH`).
Muốn dùng thêm local Bot API server thì build theo phần [Giới hạn](#giới-hạn).

### Nếu token bị lộ

Vào @BotFather → `/revoke` → chọn bot để lấy token mới, rồi sửa `BOT_TOKEN` trong `.env` ở máy đang chạy bot.
