# Văn phòng Claude

Một văn phòng pixel chạy trên máy Mac, vẽ mọi phiên Code của app Claude desktop (kể cả nhiều bản app
chạy song song) và phiên Codex trong app ChatGPT. Mỗi dự án là một phòng, mỗi phiên là một nhân viên
ngồi bàn: nhìn một lượt là biết phiên nào đang chạy, phiên nào đang chờ bạn trả lời, phiên nào xong
cần xem. Bấm vào bàn để đọc tóm tắt, trả lời ngay tại chỗ hoặc mở đúng phiên trong đúng app.

*A local pixel-office dashboard for Claude Code desktop sessions (and Codex desktop threads) on macOS.
UI in Vietnamese.*

## Cần có

- macOS 14 trở lên, app Claude desktop có dùng tab Code.
- `python3` (chỉ thư viện chuẩn) cho máy chủ; `swiftc` (Xcode Command Line Tools) và Pillow để build app.
- Tuỳ chọn:
  - `claude` CLI: trả lời và compact phiên đã đóng.
  - [claude-swap](https://pypi.org/project/claude-swap/): tab Quota hiện kho nick và hỏi số quota sống.
  - App ChatGPT có Codex: hiện phiên Codex.

## Chạy

```bash
bash build.sh        # dựng ~/Applications/Văn phòng Claude.app rồi mở (cửa sổ riêng)
bash chay.sh         # hoặc mở trong trình duyệt ở http://127.0.0.1:4747
bash chay.sh --tat   # tắt máy chủ đang chạy
```

App mang theo bản sao `server.py`, `collect.py`, `tools/` và `web/` trong gói, tự bật máy chủ khi mở và
tắt khi thoát; số phiên đang chờ bạn hiện thành chấm đỏ trên Dock. Sửa mã xong thì chạy lại `build.sh`.
Lần đầu bấm "Mở trong app", macOS hỏi quyền điều khiển Claude: chọn Cho phép.

## Trạng thái từng bàn

| Bàn | Nghĩa | Lấy từ |
|---|---|---|
| ❗ Chờ bạn (giơ tay, đỏ) | app chấm lượt cuối là `blocked` hoặc có việc cần bạn làm | `postTurnSummary` trong bản ghi phiên của app |
| Đang làm (gõ phím) | phiên đang chạy một lượt, bong bóng ghi việc đang làm | `~/.claude/sessions/<pid>.json` `status=busy` + nhật ký phiên |
| ✓ Xong, xem giúp (vàng) | app chấm `review_ready` | `postTurnSummary` |
| Rảnh | phiên mở trong app, không chạy | tiến trình còn sống, `status=idle` |
| Đã đóng (mờ) | không còn tiến trình, có hoạt động trong khung giờ đang xem | bản ghi phiên |

Áo màu theo bản app (01 xanh dương, 02 tím, 03 xanh lá, 04 hồng), routine là robot, Codex đen trắng.
Phiên con ngồi chung nhóm với phiên cha; subagent là bàn nhỏ cạnh phiên cha (đang chạy thì bay nhẹ,
xong trong 15 phút gần nhất thì mờ kèm ✓). Phiên đã lưu trữ trong app không hiện.

## Bấm vào một bàn

- **Tóm tắt**: tóm tắt nhanh của app, hoặc bộ tóm tắt của bạn nếu đã khai (xem Cấu hình).
- **Trả lời**: phiên đang mở thì đẩy thẳng vào cửa sổ qua ổ cắm nhắn tin giữa các phiên của Claude Code
  (phiên nhận cần bật `crossSessionInbound: "accept"`); phiên đã đóng thì chạy tiếp bằng
  `claude --resume <id> -p`.
- **Mở trong app**: gửi `claude://code/continue?session=local_…` tới ĐÚNG tiến trình của bản app bằng sự
  kiện Apple theo PID (`tools/gui-link-toi-pid.js`); bản đang tắt thì bật lên trước.
- **💰 Tiền phiên**: tính theo giá API như Claude Code: đọc mọi lượt gọi trong nhật ký phiên và subagent,
  ghi cache ×1,25, đọc cache ×0,1, chế độ nhanh ×2. Gói đăng ký không trả theo token, đây là giá quy đổi.
- **🧠 Ngữ cảnh** so với mốc tự tóm tắt (`autoCompactWindow` trong `~/.claude/settings.json`; không đặt
  thì so với cửa sổ ngữ cảnh của model) và **⏱ Cache ấm còn** bao lâu (TTL 1 giờ hoặc 5 phút, đọc từ nhật ký).
- **🆔 Copy ID**, **📋 Copy handoff** (bản bàn giao Markdown để dán vào phiên mới), **🗜 Compact** (phiên
  đang mở: chép sẵn `/compact` rồi mở phiên; phiên đã đóng: bấm 2 lần, chạy bằng CLI).

## Đánh dấu, bộ lọc, màn hình đã lưu

- ✓ **Xong** (xếp lên kệ cuối phòng) hoặc 🙈 **Ẩn**; "🧹 Dọn bàn đã đóng" đánh dấu cả phòng; mọi thao tác
  có Hoàn tác. Phiên có hoạt động mới sau lúc đánh dấu thì tự hiện lại.
- Lọc theo trạng thái, bản app, dự án, khung giờ (1 giờ tới 14 ngày), routine, việc đã xong, chữ tìm
  (phím `/`). "＋ Lưu màn hình này" lưu cả bộ lọc thành một nút; bộ lọc cũng nằm trên địa chỉ trang.

## Phiên Codex

Phiên mở từ app Codex trong ChatGPT, đọc chỉ đọc từ `~/.codex/state_N.sqlite` (danh sách, đã đọc hay
chưa) và `~/.codex/thread_history_N.sqlite` (từng lượt, từng mục). Lượt `codex exec` chạy từ dòng lệnh
không hiện. Trả lời: app Codex giữ khoá ghi của phiên, nên nút "Chép & mở" chép câu trả lời rồi mở đúng
phiên (`codex://threads/<id>`), bạn dán rồi Enter.

## Dự án (Beta)

Mục "Dự án" của app Claude chạy phiên điều phối và các luồng việc trên máy ảo đám mây của Anthropic, nên trên
máy không có nhật ký. Văn phòng hỏi máy chủ đúng các đường app dùng (`/v1/code/channels`,
`/v1/code/sessions`) bằng token của chính các phiên app đang mở (chỉ đọc trong bộ nhớ, không in, không lưu),
trong luồng nền, tối đa 45 giây một lần và chỉ khi trang đang mở. Mỗi dự án là một phòng: bàn "Điều phối"
cộng các luồng việc đang chạy, đang chờ bạn hoặc lỗi; luồng chạy trên chính máy này đã có bàn riêng nên không
hiện lại. "Mở dự án trong app" mở đúng trang dự án ở bản app đăng nhập nick đó; trả lời thì chép rồi mở như
Codex. Không có phiên app nào đang mở của nick đó thì phòng dự án không hiện.

## Tab Quota

Gom mọi túi quota (một email trong một tổ chức) bằng `collect.py`: nhóm **Kho nick** (claude-swap) và
**Đăng nhập trong app** (nick đang đăng nhập ở từng bản app, gắn 🗄 khi nick đó có cả trong kho). Mỗi thẻ
có vòng 5 giờ và Tuần kèm giờ hồi, thanh quota theo model, độ tuổi của số. Túi đang có phiên mở trong app
được hỏi Anthropic bằng token của chính phiên đó (chỉ đọc trong bộ nhớ, không in, không lưu), tối đa 10
phút một lần; cần cài claude-swap.

## Cấu hình

Mọi thứ riêng của máy nằm ở `~/.claude/van-phong/cau-hinh.json`, không nằm trong repo. Mọi khoá đều tuỳ
chọn; mẫu ở [`cau-hinh.mau.json`](cau-hinh.mau.json).

| Khoá | Dùng để |
|---|---|
| `duAn` | tên hiển thị và biểu tượng của từng phòng, khoá là tên thư mục repo |
| `banApp` | số bản cho thư mục dữ liệu app đặt tên khác `Claude` / `Claude-NN` (vd. `{"Claude-khac": "02"}`) |
| `moApp` | app dùng để bật một bản đang tắt (mặc định 01 là Claude.app, NN là `~/Applications/Claude-NN.app`) |
| `tomTat` | bộ tóm tắt: `{"lenh": ["claude", "-p", "--model", "haiku"]}` (lời nhắc vào stdin, JSON ra stdout) hoặc `{"hook": "đường/dẫn/module.py"}` (module có hàm `tom_tat(user, last)` trả `(dict, tên)`) |
| `tienToTraLoi` | dòng đầu gắn vào câu trả lời gửi từ văn phòng |
| `quyenTraLoi` | `--permission-mode` khi chạy tiếp phiên đã đóng (mặc định `default`) |
| `mauTeam` | màu chip theo tên tổ chức: `tim`, `cam`, `xanh`, `hong`, `vang`, `canhan` |
| `boQuaLane` | thư mục `~/.claude-*` không phải nơi đăng nhập CLI thật |
| `claude` | đường dẫn lệnh `claude` nếu không nằm trên PATH |

Nhiều bản app chạy song song: mỗi bản là Claude.app chạy với `--user-data-dir=~/Library/Application
Support/Claude-NN`; văn phòng nhận bản theo đúng thư mục đó.

## An toàn và riêng tư

- Máy chủ chỉ nghe ở `127.0.0.1`. Lệnh ghi cần header `X-Van-Phong: 1` và nguồn là chính trang này, nên
  trang web lạ không gọi được (trình duyệt phải hỏi CORS trước mà máy chủ không trả lời).
- Chỉ ĐỌC dữ liệu app tự ghi, không sửa file nào của app Claude hay Codex. Chỉ ghi vào
  `~/.claude/van-phong/` (đánh dấu, màn hình đã lưu, đệm tóm tắt, tên tổ chức, log) và
  `~/.cache/claude-quota/live.json` (chỉ số %).
- Không gửi gì ra ngoài, trừ: bộ tóm tắt bạn tự khai, lượt hỏi quota tới `api.anthropic.com` của tab
  Quota (cần claude-swap), và lượt đọc danh sách Dự án tới `api.anthropic.com` (chỉ đọc).

## Ảnh

Nhân vật và đồ đạc vẽ bằng model tạo ảnh rồi cắt thành sprite nền trong suốt bằng
`python3 tools/cat-sprite.py` (ảnh gốc `web/assets/raw/` không nằm trong repo).

## Giấy phép

MIT, xem [LICENSE](LICENSE).
