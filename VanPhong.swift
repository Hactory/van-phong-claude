// Văn phòng Claude: cửa sổ riêng bọc trang http://127.0.0.1:4747.
// Mở app = bật máy chủ server.py (nằm trong Resources) nếu chưa chạy; tắt app = tắt máy chủ do app bật.
// Số phiên đang chờ bạn hiện thành chấm đỏ trên biểu tượng Dock (đọc từ tiêu đề trang "(N) Văn phòng Claude").
// Thanh tiêu đề, ngang hàng 3 nút cửa sổ, có một dòng tình trạng máy: CPU, RAM, phần Claude, Codex, Antigravity đang tốn,
// chỗ trống từng ổ.
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
    let machine = MachineStats()
    var machineLabel: NSTextField!
    var machineTimer: Timer?

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
        window.titleVisibility = .hidden   // trang đã có tên văn phòng; thanh tiêu đề nhường chỗ cho dòng tình trạng máy
        window.titlebarAppearsTransparent = true
        window.backgroundColor = NSColor(red: 0.90, green: 0.96, blue: 0.93, alpha: 1)
        window.minSize = NSSize(width: 420, height: 480)
        window.contentView = web
        window.center()
        window.setFrameAutosaveName("VanPhongClaude")
        setupMachineBar()
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

    // MARK: dòng tình trạng máy trên thanh tiêu đề

    func setupMachineBar() {
        machineLabel = NSTextField(labelWithString: "")
        machineLabel.font = .monospacedDigitSystemFont(ofSize: 11.5, weight: .semibold)
        machineLabel.lineBreakMode = .byTruncatingTail
        machineLabel.translatesAutoresizingMaskIntoConstraints = false
        let box = NSView(frame: NSRect(x: 0, y: 0, width: 980, height: 28))
        box.addSubview(machineLabel)
        NSLayoutConstraint.activate([
            machineLabel.leadingAnchor.constraint(equalTo: box.leadingAnchor, constant: 10),
            machineLabel.trailingAnchor.constraint(lessThanOrEqualTo: box.trailingAnchor),
            machineLabel.centerYAnchor.constraint(equalTo: box.centerYAnchor),
        ])
        let acc = NSTitlebarAccessoryViewController()
        acc.layoutAttribute = .leading      // ngay sau 3 nút đóng, thu nhỏ, toàn màn hình
        acc.view = box
        window.addTitlebarAccessoryViewController(acc)
        refreshMachineBar()
        machineTimer = Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { [weak self] _ in self?.refreshMachineBar() }
        machineTimer?.tolerance = 0.5
    }

    func refreshMachineBar() {
        // Cửa sổ khuất hẳn (thu nhỏ, bị che) thì khỏi đo; CPU lần sau tính trung bình cả khoảng nghỉ.
        guard window.occlusionState.contains(.visible) else { return }
        let r = machine.read()
        machineLabel.attributedStringValue = r.line
        machineLabel.superview?.toolTip = r.detail
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

// MARK: - Đo tình trạng máy (đọc thẳng từ hệ điều hành, không chạy lệnh ngoài)

final class MachineStats {
    private var prevTicks: [UInt32]?
    private var disks: [(label: String, free: Int64, total: Int64)] = []
    private var disksAt = Date.distantPast
    private let apps = AppUsage()

    static let ink = NSColor(red: 0.42, green: 0.31, blue: 0.23, alpha: 1)
    static let warn = NSColor(red: 0.82, green: 0.47, blue: 0.04, alpha: 1)
    static let bad = NSColor(red: 0.80, green: 0.17, blue: 0.19, alpha: 1)

    /// % CPU cả máy giữa hai lần đọc (tổng mọi nhân, từ bộ đếm tick của nhân hệ điều hành).
    func cpuPercent() -> Double? {
        var info = host_cpu_load_info()
        var count = mach_msg_type_number_t(MemoryLayout<host_cpu_load_info_data_t>.stride / MemoryLayout<integer_t>.stride)
        let kr = withUnsafeMutablePointer(to: &info) {
            $0.withMemoryRebound(to: integer_t.self, capacity: Int(count)) { host_statistics(mach_host_self(), HOST_CPU_LOAD_INFO, $0, &count) }
        }
        guard kr == KERN_SUCCESS else { return nil }
        let t = [info.cpu_ticks.0, info.cpu_ticks.1, info.cpu_ticks.2, info.cpu_ticks.3]   // user, system, idle, nice
        defer { prevTicks = t }
        guard let p = prevTicks else { return nil }
        let d = zip(t, p).map { Double($0 &- $1) }
        let total = d.reduce(0, +)
        return total > 0 ? (total - d[2]) / total * 100 : nil
    }

    /// RAM đang dùng kiểu Activity Monitor: bộ nhớ app + bộ nhớ hệ thống giữ cứng + phần đã nén.
    func memory() -> (used: UInt64, app: UInt64, wired: UInt64, compressed: UInt64)? {
        var stats = vm_statistics64()
        var count = mach_msg_type_number_t(MemoryLayout<vm_statistics64_data_t>.stride / MemoryLayout<integer_t>.stride)
        let kr = withUnsafeMutablePointer(to: &stats) {
            $0.withMemoryRebound(to: integer_t.self, capacity: Int(count)) { host_statistics64(mach_host_self(), HOST_VM_INFO64, $0, &count) }
        }
        guard kr == KERN_SUCCESS else { return nil }
        var pageSize: vm_size_t = 0
        host_page_size(mach_host_self(), &pageSize)
        let page = UInt64(pageSize)
        let app = UInt64(stats.internal_page_count > stats.purgeable_count ? stats.internal_page_count - stats.purgeable_count : 0) * page
        let wired = UInt64(stats.wire_count) * page
        let comp = UInt64(stats.compressor_page_count) * page
        return (app + wired + comp, app, wired, comp)
    }

    /// Mức áp lực bộ nhớ của macOS: 1 bình thường, 2 cảnh báo, 4 nguy cấp (màu của biểu đồ trong Activity Monitor).
    func memoryPressure() -> Int32 {
        var level: Int32 = 0
        var size = MemoryLayout<Int32>.size
        return sysctlbyname("kern.memorystatus_vm_pressure_level", &level, &size, nil, 0) == 0 ? level : 0
    }

    func swapUsed() -> UInt64 {
        var sw = xsw_usage()
        var size = MemoryLayout<xsw_usage>.size
        return sysctlbyname("vm.swapusage", &sw, &size, nil, 0) == 0 ? sw.xsu_used : 0
    }

    /// Ổ hiện trên thanh: khai trong ~/.claude/van-phong/cau-hinh.json mục "oDia" (danh sách đường dẫn), không khai
    /// thì lấy ổ máy cộng các ổ gắn ngoài ghi được (bỏ phân vùng Time Machine và Recovery). Đọc lại mỗi 15 giây.
    func diskList() -> [(label: String, free: Int64, total: Int64)] {
        if Date().timeIntervalSince(disksAt) < 15 { return disks }
        disksAt = Date()
        let keys: [URLResourceKey] = [.volumeNameKey, .volumeIsLocalKey, .volumeIsReadOnlyKey, .volumeIsBrowsableKey,
                                      .volumeIsRootFileSystemKey, .volumeAvailableCapacityForImportantUsageKey,
                                      .volumeAvailableCapacityKey, .volumeTotalCapacityKey]
        var urls: [URL]
        let cfgPath = (NSHomeDirectory() as NSString).appendingPathComponent(".claude/van-phong/cau-hinh.json")
        if let data = FileManager.default.contents(atPath: cfgPath),
           let cfg = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           let list = cfg["oDia"] as? [String], !list.isEmpty {
            urls = list.map { URL(fileURLWithPath: ($0 as NSString).expandingTildeInPath) }
        } else {
            urls = [URL(fileURLWithPath: "/")]
            for u in FileManager.default.mountedVolumeURLs(includingResourceValuesForKeys: keys, options: [.skipHiddenVolumes]) ?? [] {
                guard let v = try? u.resourceValues(forKeys: Set(keys)), v.volumeIsLocal == true, v.volumeIsReadOnly != true,
                      v.volumeIsBrowsable == true, v.volumeIsRootFileSystem != true else { continue }
                let name = (v.volumeName ?? "").lowercased()
                if name.contains("time machine") || name.contains("timemachine") || name == "recovery" { continue }
                urls.append(u)
            }
        }
        disks = urls.compactMap { u in
            guard let v = try? u.resourceValues(forKeys: Set(keys)), let total = v.volumeTotalCapacity, total > 0 else { return nil }
            let free = v.volumeAvailableCapacityForImportantUsage ?? Int64(v.volumeAvailableCapacity ?? 0)
            let label = v.volumeIsRootFileSystem == true ? "Ổ máy" : (v.volumeName ?? u.lastPathComponent)
            return (label, free, Int64(total))
        }
        return disks
    }

    private static let formatters: [NumberFormatter] = (0...2).map { d in
        let f = NumberFormatter()
        f.locale = Locale(identifier: "vi_VN")
        f.minimumFractionDigits = d
        f.maximumFractionDigits = d
        return f
    }

    static func num(_ x: Double, _ digits: Int) -> String {
        formatters[min(max(digits, 0), 2)].string(from: NSNumber(value: x)) ?? String(format: "%.\(digits)f", x)
    }

    static func bytes(_ b: Int64) -> String {   // dung lượng ổ tính theo 1000 như Finder
        let gb = Double(b) / 1e9
        return gb >= 1000 ? "\(num(gb / 1000, 2)) TB" : "\(num(gb, gb < 100 ? 1 : 0)) GB"
    }

    func read() -> (line: NSAttributedString, detail: String) {
        let out = NSMutableAttributedString()
        let font = NSFont.monospacedDigitSystemFont(ofSize: 11.5, weight: .semibold)
        func add(_ text: String, _ color: NSColor = MachineStats.ink) {
            out.append(NSAttributedString(string: text, attributes: [.foregroundColor: color, .font: font]))
        }
        func sep() { add("  ·  ", MachineStats.ink.withAlphaComponent(0.45)) }
        var detail: [String] = []

        let cpu = cpuPercent()
        add("CPU ", MachineStats.ink.withAlphaComponent(0.75))
        add(cpu.map { "\(Int($0.rounded()))%" } ?? "…", (cpu ?? 0) >= 85 ? MachineStats.bad : (cpu ?? 0) >= 60 ? MachineStats.warn : MachineStats.ink)
        var load = [Double](repeating: 0, count: 3)
        if getloadavg(&load, 3) == 3 {
            detail.append("CPU: \(ProcessInfo.processInfo.activeProcessorCount) nhân, tải trung bình 1/5/15 phút \(load.map { MachineStats.num($0, 2) }.joined(separator: " / "))")
        }

        if let m = memory() {
            let gib = 1_073_741_824.0
            let total = Double(ProcessInfo.processInfo.physicalMemory)
            let level = memoryPressure()
            let color = level >= 4 ? MachineStats.bad : (level >= 2 || Double(m.used) / total >= 0.9) ? MachineStats.warn : MachineStats.ink
            sep()
            add("RAM ", MachineStats.ink.withAlphaComponent(0.75))
            add("\(MachineStats.num(Double(m.used) / gib, 1))/\(Int((total / gib).rounded())) GB", color)
            let swap = swapUsed()
            detail.append("RAM: app \(MachineStats.num(Double(m.app) / gib, 1)) GB, hệ thống giữ \(MachineStats.num(Double(m.wired) / gib, 1)) GB, đã nén \(MachineStats.num(Double(m.compressed) / gib, 1)) GB"
                          + (swap > 0 ? ", swap \(MachineStats.num(Double(swap) / gib, 1)) GB" : "")
                          + " · áp lực bộ nhớ: " + (level >= 4 ? "nguy cấp" : level >= 2 ? "cảnh báo" : "bình thường"))
        }

        let u = apps.sample()
        let cores = Double(ProcessInfo.processInfo.activeProcessorCount)
        let gib = 1_073_741_824.0
        func pct(_ x: Double) -> String { "\(x < 10 ? MachineStats.num(x, 1) : String(Int(x.rounded())))%" }
        func gb(_ b: UInt64) -> String { "\(MachineStats.num(Double(b) / gib, 1)) GB" }
        for (fam, g) in u.groups.enumerated() where g.count > 0 {
            let (label, appName, cliName) = AppUsage.labels[fam]
            sep()
            add("\(label) ", MachineStats.ink.withAlphaComponent(0.75))
            add(u.ready ? "\(Int(g.cpu.rounded()))%" : "…", g.cpu >= 85 ? MachineStats.bad : g.cpu >= 60 ? MachineStats.warn : MachineStats.ink)
            add(", \(gb(g.mem))")
            guard u.ready else { continue }
            var lines = ["\(label): \(pct(g.cpu)) CPU cả máy (≈ \(MachineStats.num(g.cpu / 100 * cores, 1)) trên \(Int(cores)) nhân), \(gb(g.mem)) RAM, \(g.count) tiến trình"]
            let parts = [(appName, g.app), (cliName, g.cli), ("lệnh chạy hộ (build, test, MCP…)", g.kids)].filter { $0.1.count > 0 }
            lines.append("    " + parts.map { "\($0.0) \(pct($0.1.cpu)), \(gb($0.1.mem))" }.joined(separator: " · "))
            let byCPU = g.heavy.filter { $0.value.cpu >= 0.5 }.sorted { $0.value.cpu > $1.value.cpu }.prefix(4)
            if !byCPU.isEmpty { lines.append("    ngốn CPU nhất: " + byCPU.map { "\($0.key) \(pct($0.value.cpu))" }.joined(separator: ", ")) }
            let byMem = g.heavy.filter { $0.value.mem >= 200 << 20 }.sorted { $0.value.mem > $1.value.mem }.prefix(4)
            if !byMem.isEmpty { lines.append("    giữ RAM nhiều nhất: " + byMem.map { "\($0.key) \(gb($0.value.mem))" }.joined(separator: ", ")) }
            detail.append(lines.joined(separator: "\n"))
        }

        if u.ready && u.groups.contains(where: { $0.count > 0 }) {
            detail.append("(RAM của từng nhóm tính như cột Bộ nhớ của Activity Monitor, gồm cả phần macOS đã nén hoặc đẩy ra swap, nên có thể lớn hơn RAM đang dùng.)")
        }

        for d in diskList() {
            let ratio = Double(d.free) / Double(d.total)
            let color = (ratio < 0.05 || d.free < 10_000_000_000) ? MachineStats.bad
                : (ratio < 0.15 || d.free < 30_000_000_000) ? MachineStats.warn : MachineStats.ink
            sep()
            add("\(d.label) còn ", MachineStats.ink.withAlphaComponent(0.75))
            add(MachineStats.bytes(d.free), color)
            detail.append("\(d.label): trống \(MachineStats.bytes(d.free)) / \(MachineStats.bytes(d.total)) (\(Int((ratio * 100).rounded()))%)")
        }
        return (out, detail.joined(separator: "\n"))
    }
}

// MARK: - Claude, Codex, Antigravity đang tốn bao nhiêu, tính cả mọi lệnh chúng chạy hộ (build, test, máy chủ MCP…)

final class AppUsage {
    enum Kind {
        case claudeApp, claudeCLI, codexApp, codexCLI, agApp, agCLI
        /// Số thứ tự nhóm trong `labels` và trong kết quả của `sample()`.
        var family: Int {
            switch self {
            case .claudeApp, .claudeCLI: return 0
            case .codexApp, .codexCLI: return 1
            case .agApp, .agCLI: return 2
            }
        }
        var isCLI: Bool { self == .claudeCLI || self == .codexCLI || self == .agCLI }
    }
    static let labels = [("Claude", "app Claude", "phiên Claude Code"),
                         ("Codex", "app ChatGPT/Codex", "phiên Codex CLI"),
                         ("Antigravity", "app Antigravity", "phiên agy CLI")]
    struct Part { var cpu = 0.0, mem: UInt64 = 0, count = 0 }
    struct Group {
        var app = Part(), cli = Part(), kids = Part()
        var heavy: [String: (cpu: Double, mem: UInt64)] = [:]   // tên lệnh chạy hộ → % CPU cả máy, RAM
        var cpu: Double { app.cpu + cli.cpu + kids.cpu }
        var mem: UInt64 { app.mem + cli.mem + kids.mem }
        var count: Int { app.count + cli.count + kids.count }
    }

    private var prev: [pid_t: (start: UInt64, cpu: UInt64)] = [:]
    private var prevAt: UInt64 = 0      // mach_absolute_time của lần đọc trước
    private var prevWallUs: UInt64 = 0
    private var names: [pid_t: (start: UInt64, kind: Kind?, name: String)] = [:]
    private let tickNs: Double
    private let cores = Double(ProcessInfo.processInfo.activeProcessorCount)

    init() {
        var tb = mach_timebase_info_data_t()
        mach_timebase_info(&tb)
        tickNs = Double(tb.numer) / Double(tb.denom)
    }

    /// Nhận diện theo đường dẫn chương trình. Phiên Claude Code của app desktop nằm trong .../claude-code/<bản>/,
    /// bản CLI cài riêng nằm ở ~/.local/share/claude/; Codex desktop nay nằm trong ChatGPT.app; Antigravity desktop
    /// gồm cả máy chủ ngôn ngữ trong gói app, CLI của nó là lệnh `agy`.
    static func kind(_ path: String) -> Kind? {
        let name = (path as NSString).lastPathComponent
        if path.contains("/claude-code/") || path.contains("/.local/share/claude/") || path.contains("/@anthropic-ai/claude-code") || name == "claude" { return .claudeCLI }
        if path.contains("/Claude.app/") { return .claudeApp }
        if name == "codex" || path.contains("/codex-cli/") || path.contains("/@openai/codex") { return .codexCLI }
        if path.contains("/ChatGPT.app/") || path.contains("/Codex.app/") || path.contains("/.codex/")
            || path.contains("/Application Support/Codex/") || path.contains("/com.openai.codex/") { return .codexApp }
        if name == "agy" { return .agCLI }
        if path.contains("/Antigravity.app/") { return .agApp }
        return nil
    }

    /// % CPU tính trên cả máy (16 nhân chạy hết = 100%) giữa hai lần đọc; RAM là "bộ nhớ" như cột Memory của Activity Monitor.
    func sample() -> (groups: [Group], ready: Bool) {
        let now = mach_absolute_time()
        let wallUs = UInt64(Date().timeIntervalSince1970 * 1e6)
        var groups = [Group](repeating: Group(), count: AppUsage.labels.count)
        let n = Int(proc_listallpids(nil, 0))
        guard n > 0 else { return (groups, false) }
        var pids = [pid_t](repeating: 0, count: n + 256)
        let got = Int(proc_listallpids(&pids, Int32(pids.count * MemoryLayout<pid_t>.stride)))

        var parent: [pid_t: pid_t] = [:], start: [pid_t: UInt64] = [:]
        let bsdSize = Int32(MemoryLayout<proc_bsdinfo>.stride)
        for pid in pids.prefix(max(0, min(got, pids.count))) where pid > 0 {
            var bsd = proc_bsdinfo()
            guard proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, &bsd, bsdSize) == bsdSize else { continue }
            parent[pid] = pid_t(bsd.pbi_ppid)
            start[pid] = bsd.pbi_start_tvsec &* 1_000_000 &+ bsd.pbi_start_tvusec
        }

        func ownKind(_ pid: pid_t) -> Kind? {
            let st = start[pid] ?? 0
            if let c = names[pid], c.start == st { return c.kind }
            var buf = [CChar](repeating: 0, count: 4096)
            let path = proc_pidpath(pid, &buf, UInt32(buf.count)) > 0 ? String(cString: buf) : ""
            let k = AppUsage.kind(path)
            names[pid] = (st, k, (path as NSString).lastPathComponent)
            return k
        }
        // Tiến trình thuộc về một nhóm nếu chính nó, hoặc gần nhất trong các đời cha, là app/CLI của nhóm đó.
        var memo: [pid_t: Kind?] = [:]
        func owner(_ pid: pid_t) -> Kind? {
            var chain: [pid_t] = []
            var p = pid
            var found: Kind? = nil
            while p > 1, chain.count < 64 {
                if let m = memo[p] { found = m; break }
                chain.append(p)
                if let k = ownKind(p) { found = k; break }
                guard let pp = parent[p] else { break }
                p = pp
            }
            for c in chain { memo.updateValue(found, forKey: c) }
            return found
        }

        let ready = prevAt > 0
        let span = Double(now &- prevAt) * tickNs * cores
        var next: [pid_t: (start: UInt64, cpu: UInt64)] = [:]
        for (pid, st) in start {
            guard let who = owner(pid) else { continue }
            var ru = rusage_info_v2()
            let rc = withUnsafeMutablePointer(to: &ru) {
                $0.withMemoryRebound(to: rusage_info_t?.self, capacity: 1) { proc_pid_rusage(pid, RUSAGE_INFO_V2, $0) }
            }
            guard rc == 0 else { continue }
            let total = ru.ri_user_time &+ ru.ri_system_time   // đơn vị tick của mach_absolute_time
            next[pid] = (st, total)
            var used: UInt64 = 0
            if let p = prev[pid], p.start == st { used = total >= p.cpu ? total - p.cpu : 0 }
            else if ready && st >= prevWallUs { used = total }   // sinh ra sau lần đọc trước
            let share = ready && span > 0 ? Double(used) * tickNs / span * 100 : 0
            var g = groups[who.family]
            switch ownKind(pid) {
            case let k? where k.isCLI:
                g.cli.cpu += share; g.cli.mem += ru.ri_phys_footprint; g.cli.count += 1
            case .some:
                g.app.cpu += share; g.app.mem += ru.ri_phys_footprint; g.app.count += 1
            case nil:
                g.kids.cpu += share; g.kids.mem += ru.ri_phys_footprint; g.kids.count += 1
                let name = names[pid]?.name ?? ""
                let key = name.isEmpty ? "?" : name
                let h = g.heavy[key] ?? (0, 0)
                g.heavy[key] = (h.cpu + share, h.mem + ru.ri_phys_footprint)
            }
            groups[who.family] = g
        }
        prev = next
        prevAt = now
        prevWallUs = wallUs
        names = names.filter { start[$0.key] == $0.value.start }
        return (groups, ready)
    }
}
