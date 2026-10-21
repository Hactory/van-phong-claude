// gui-link-toi-pid.js — gửi một link claude:// tới ĐÚNG MỘT tiến trình Claude theo PID.
// Vì sao cần: nhiều bản Claude desktop (mỗi bản một --user-data-dir) chạy chung Claude.app, cùng bundle id,
// nên khi macOS tự chuyển link thì không chọn được bản nào nhận. Sự kiện Apple 'GURL' gửi theo PID
// đi thẳng vào app.on("open-url") của đúng bản đó.
// Cách gọi: osascript -l JavaScript gui-link-toi-pid.js <pid> <url>
ObjC.import('Foundation');
function run(argv) {
  var pid = parseInt(argv[0], 10), url = argv[1];
  if (!pid || !url) throw new Error('Cách gọi: gui-link-toi-pid.js <pid> <url>');
  var GURL = 0x4755524c, KEY_DIRECT = 0x2d2d2d2d; // 'GURL', '----'
  var target = $.NSAppleEventDescriptor.descriptorWithProcessIdentifier(pid);
  var ev = $.NSAppleEventDescriptor.appleEventWithEventClassEventIDTargetDescriptorReturnIDTransactionID(
    GURL, GURL, target, -1, 0);
  ev.setParamDescriptorForKeyword($.NSAppleEventDescriptor.descriptorWithString(url), KEY_DIRECT);
  var err = Ref();
  var res = ev.sendEventWithOptionsTimeoutError(0x1 /* NoReply */, 15, err);
  if (res.isNil()) throw new Error('Gửi không được: ' + ObjC.unwrap(err[0].localizedDescription));
  return 'đã gửi tới PID ' + pid;
}
