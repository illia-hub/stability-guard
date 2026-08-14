// Small banner-style panel for stability-guard.
//
// Why this exists: macOS hides real notification banners while the screen is
// being recorded or a Focus is active, and an AppleScript dialog steals the
// keyboard and is huge. This panel is a non-activating window, so it can never
// take focus or swallow input, and it closes itself after a few seconds.
//
// Usage: sgnotify "<title>" "<message>" [seconds] [stack-index]

import Cocoa

let args = CommandLine.arguments
guard args.count >= 3 else {
    FileHandle.standardError.write("usage: sgnotify <title> <message> [seconds] [index]\n".data(using: .utf8)!)
    exit(1)
}

let title = args[1]
let message = args[2]
let seconds = args.count > 3 ? (Double(args[3]) ?? 4) : 4
let stackIndex = args.count > 4 ? (Int(args[4]) ?? 0) : 0

let app = NSApplication.shared
// .accessory keeps it out of the Dock and the app switcher entirely.
app.setActivationPolicy(.accessory)

let width: CGFloat = 340
let padding: CGFloat = 14
let titleFont = NSFont.systemFont(ofSize: 13, weight: .semibold)
let bodyFont = NSFont.systemFont(ofSize: 12)

// Measure the text so the panel is exactly as tall as it needs to be.
func height(of text: String, font: NSFont, width: CGFloat) -> CGFloat {
    let attrs: [NSAttributedString.Key: Any] = [.font: font]
    let bounds = (text as NSString).boundingRect(
        with: NSSize(width: width, height: 400),
        options: [.usesLineFragmentOrigin, .usesFontLeading],
        attributes: attrs)
    return ceil(bounds.height)
}

let textWidth = width - padding * 2
let titleHeight = height(of: title, font: titleFont, width: textWidth)
let bodyHeight = min(height(of: message, font: bodyFont, width: textWidth), 120)
let panelHeight = padding * 2 + titleHeight + 6 + bodyHeight

guard let screen = NSScreen.main else { exit(1) }
let visible = screen.visibleFrame
let margin: CGFloat = 12
// Stack multiple panels downwards so they never cover each other.
let offsetY = CGFloat(stackIndex) * (panelHeight + 8)
let frame = NSRect(x: visible.maxX - width - margin,
                   y: visible.maxY - panelHeight - margin - offsetY,
                   width: width,
                   height: panelHeight)

// .nonactivatingPanel is the important bit: the window never becomes key, so
// your typing keeps going to whatever app you were using.
let panel = NSPanel(contentRect: frame,
                    styleMask: [.borderless, .nonactivatingPanel],
                    backing: .buffered,
                    defer: false)
panel.isFloatingPanel = true
panel.level = .statusBar
panel.backgroundColor = .clear
panel.isOpaque = false
panel.hasShadow = true
panel.ignoresMouseEvents = true          // clicks pass straight through
panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .ignoresCycle]

let blur = NSVisualEffectView(frame: NSRect(origin: .zero, size: frame.size))
blur.material = .hudWindow
blur.blendingMode = .behindWindow
blur.state = .active
blur.wantsLayer = true
blur.layer?.cornerRadius = 12
blur.layer?.masksToBounds = true

let titleLabel = NSTextField(labelWithString: title)
titleLabel.font = titleFont
titleLabel.textColor = .labelColor
titleLabel.frame = NSRect(x: padding,
                          y: panelHeight - padding - titleHeight,
                          width: textWidth,
                          height: titleHeight)

let bodyLabel = NSTextField(wrappingLabelWithString: message)
bodyLabel.font = bodyFont
bodyLabel.textColor = .secondaryLabelColor
bodyLabel.frame = NSRect(x: padding, y: padding, width: textWidth, height: bodyHeight)

blur.addSubview(titleLabel)
blur.addSubview(bodyLabel)
panel.contentView = blur

panel.alphaValue = 0
panel.orderFrontRegardless()              // show without activating the app

NSAnimationContext.runAnimationGroup { ctx in
    ctx.duration = 0.18
    panel.animator().alphaValue = 1
}

DispatchQueue.main.asyncAfter(deadline: .now() + seconds) {
    NSAnimationContext.runAnimationGroup({ ctx in
        ctx.duration = 0.25
        panel.animator().alphaValue = 0
    }, completionHandler: {
        exit(0)
    })
}

app.run()
