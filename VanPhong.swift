// Văn phòng Claude: cửa sổ riêng bọc trang http://127.0.0.1:4747.
// Mở app = bật máy chủ server.py (nằm trong Resources) nếu chưa chạy; tắt app = tắt máy chủ do app bật.
// Số phiên đang chờ bạn hiện thành chấm đỏ trên biểu tượng Dock (đọc từ tiêu đề trang "(N) Văn phòng Claude").
import AppKit
import WebKit

let PORT = ProcessInfo.processInfo.environment["VAN_PHONG_PORT"] ?? "4747"
let HOME_URL = URL(string: "http://127.0.0.1:\(PORT)/")!

@main
final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate {
    var window: NSWindow!
    var web: WKWebView!
    var server: Process?
    var titleObserver: NSKeyValueObservation?

    static func main() {
        let app = NSApplication.shared
        let delegate = AppDelegate()
        app.delegate = delegate
        app.setActivationPolicy(.regular)
        app.run()
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        buildMenu()
        let config = WKWebViewConfiguration()
        config.websiteDataStore = .default()  // giữ localStorage giữa các lần mở
        web = WKWebView(frame: .zero, configuration: config)
        web.navigationDelegate = self
        web.uiDelegate = self
        web.allowsBackForwardNavigationGestures = false
        web.setValue(false, forKey: "drawsBackground")

        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1440, height: 920),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        window.title = "Văn phòng Claude"
        window.titlebarAppearsTransparent = true
        window.backgroundColor = NSColor(red: 0.90, green: 0.96, blue: 0.93, alpha: 1)
        window.minSize = NSSize(width: 420, height: 480)
        window.contentView = web
        window.center()
        window.setFrameAutosaveName("VanPhongClaude")
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)

        titleObserver = web.observe(\.title, options: [.new]) { _, change in
            let title = (change.newValue ?? nil) ?? ""
            let m = title.range(of: #"^\((\d+)\)"#, options: .regularExpression)
            NSApp.dockTile.badgeLabel = m.map { String(title[$0].dropFirst().dropLast()) }
        }

        showLoading("Đang mở cửa văn phòng…")
        ensureServer { [weak self] ok in
            guard let self else { return }
            if ok { self.web.load(URLRequest(url: HOME_URL)) }
            else { self.showLoading("Máy chủ không lên được. Xem ~/.claude/van-phong/server.out") }
        }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    func applicationWillTerminate(_ notification: Notification) {
        server?.terminate()  // chỉ tắt máy chủ do chính app bật
    }

    // MARK: máy chủ

    func serverAlive(_ done: @escaping (Bool) -> Void) {
        var req = URLRequest(url: HOME_URL.appendingPathComponent("api/views"))
        req.timeoutInterval = 1.5
        URLSession.shared.dataTask(with: req) { _, resp, _ in
            done((resp as? HTTPURLResponse)?.statusCode == 200)
        }.resume()
    }

    func ensureServer(_ done: @escaping (Bool) -> Void) {
        serverAlive { alive in
            DispatchQueue.main.async {
                if alive { return done(true) }
                self.startServer()
                self.waitServer(tries: 40, done)
            }
        }
    }

    func waitServer(tries: Int, _ done: @escaping (Bool) -> Void) {
        serverAlive { alive in
            DispatchQueue.main.asyncAfter(deadline: .now() + (alive ? 0 : 0.5)) {
                if alive { done(true) } else if tries <= 0 { done(false) } else { self.waitServer(tries: tries - 1, done) }
            }
        }
    }

    func startServer() {
        guard let script = Bundle.main.path(forResource: "server", ofType: "py") else { return }
        let python = ["/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/usr/bin/python3"]
            .first { FileManager.default.isExecutableFile(atPath: $0) } ?? "/usr/bin/python3"
        let stateDir = (NSHomeDirectory() as NSString).appendingPathComponent(".claude/van-phong")
        try? FileManager.default.createDirectory(atPath: stateDir, withIntermediateDirectories: true)
        let logPath = (stateDir as NSString).appendingPathComponent("server.out")
        if !FileManager.default.fileExists(atPath: logPath) { FileManager.default.createFile(atPath: logPath, contents: nil) }
        let log = FileHandle(forWritingAtPath: logPath)
        log?.seekToEndOfFile()
        let p = Process()
        p.executableURL = URL(fileURLWithPath: python)
        p.arguments = [script, "--port", PORT]
        var env = ProcessInfo.processInfo.environment
        env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:\(NSHomeDirectory())/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        p.environment = env
        p.standardOutput = log
        p.standardError = log
        do { try p.run(); server = p } catch { NSLog("Văn phòng: không bật được máy chủ: \(error)") }
    }

    func showLoading(_ text: String) {
        let html = """
        <html><body style="margin:0;height:100vh;display:flex;align-items:center;justify-content:center;
        background:#e6f4ec;font:600 16px -apple-system;color:#7c6a5d">\(text)</body></html>
        """
        web.loadHTMLString(html, baseURL: nil)
    }

    // MARK: liên kết ngoài mở bằng trình duyệt mặc định

    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if let url = action.request.url, let host = url.host, !["127.0.0.1", "localhost"].contains(host),
           action.navigationType == .linkActivated || action.targetFrame == nil {
            NSWorkspace.shared.open(url)
            return decisionHandler(.cancel)
        }
        decisionHandler(.allow)
    }

    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = action.request.url { NSWorkspace.shared.open(url) }
        return nil
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) { retryLater() }
    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) { retryLater() }

    func retryLater() {
        showLoading("Mất kết nối máy chủ văn phòng, đang thử lại…")
        DispatchQueue.main.asyncAfter(deadline: .now() + 2) {
            self.ensureServer { ok in if ok { self.web.load(URLRequest(url: HOME_URL)) } else { self.retryLater() } }
        }
    }

    // MARK: menu (cần Edit để ⌘C/⌘V chạy trong ô trả lời)

    @objc func reload() { web.load(URLRequest(url: HOME_URL)) }
    @objc func openInBrowser() { NSWorkspace.shared.open(web.url ?? HOME_URL) }

    func buildMenu() {
        let main = NSMenu()
        let appItem = NSMenuItem(); main.addItem(appItem)
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "Ẩn Văn phòng Claude", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Thoát Văn phòng Claude", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu

        let editItem = NSMenuItem(); main.addItem(editItem)
        let edit = NSMenu(title: "Sửa")
        edit.addItem(withTitle: "Hoàn tác", action: Selector(("undo:")), keyEquivalent: "z")
        edit.addItem(withTitle: "Làm lại", action: Selector(("redo:")), keyEquivalent: "Z")
        edit.addItem(.separator())
        edit.addItem(withTitle: "Cắt", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        edit.addItem(withTitle: "Chép", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: "Dán", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        edit.addItem(withTitle: "Chọn tất cả", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = edit

        let viewItem = NSMenuItem(); main.addItem(viewItem)
        let view = NSMenu(title: "Xem")
        view.addItem(withTitle: "Tải lại", action: #selector(reload), keyEquivalent: "r")
        view.addItem(withTitle: "Mở trong trình duyệt", action: #selector(openInBrowser), keyEquivalent: "o")
        view.addItem(withTitle: "Toàn màn hình", action: #selector(NSWindow.toggleFullScreen(_:)), keyEquivalent: "f")
        viewItem.submenu = view

        let winItem = NSMenuItem(); main.addItem(winItem)
        let win = NSMenu(title: "Cửa sổ")
        win.addItem(withTitle: "Thu nhỏ", action: #selector(NSWindow.miniaturize(_:)), keyEquivalent: "m")
        win.addItem(withTitle: "Đóng", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        winItem.submenu = win
        NSApp.windowsMenu = win
        NSApp.mainMenu = main
    }
}
