// Build-only icon artwork. No network, app data, fonts or provider branding.
import AppKit

let output = CommandLine.arguments.dropFirst().first ?? "assets/AgentBoard.iconset"
let directory = URL(fileURLWithPath: output, isDirectory: true)
try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)

func render(_ pixels: Int, to url: URL) throws {
    let bitmap = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: pixels, pixelsHigh: pixels,
                                  bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true,
                                  isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: bitmap)
    let context = NSGraphicsContext.current!.cgContext
    context.scaleBy(x: CGFloat(pixels) / 1024, y: CGFloat(pixels) / 1024)
    context.setAllowsAntialiasing(true)
    let tile = NSBezierPath(roundedRect: NSRect(x: 72, y: 72, width: 880, height: 880), xRadius: 195, yRadius: 195)
    NSColor(calibratedRed: 0.075, green: 0.115, blue: 0.085, alpha: 1).setFill()
    tile.fill()
    let board = NSBezierPath(roundedRect: NSRect(x: 220, y: 244, width: 584, height: 536), xRadius: 58, yRadius: 58)
    NSColor(calibratedRed: 0.76, green: 0.88, blue: 0.73, alpha: 1).setStroke()
    board.lineWidth = 24
    board.stroke()
    // Three persistent status rows remain legible in a small Dock icon.
    for (index, y) in [CGFloat(650), CGFloat(512), CGFloat(374)].enumerated() {
        let color = index == 1
            ? NSColor(calibratedRed: 0.91, green: 0.72, blue: 0.35, alpha: 1)
            : NSColor(calibratedRed: 0.57, green: 0.80, blue: 0.53, alpha: 1)
        color.setFill()
        NSBezierPath(ovalIn: NSRect(x: 282, y: y - 24, width: 48, height: 48)).fill()
        NSColor(calibratedRed: 0.76, green: 0.88, blue: 0.73, alpha: 1).setFill()
        NSBezierPath(roundedRect: NSRect(x: 378, y: y - 13, width: index == 1 ? 226 : 302, height: 26), xRadius: 13, yRadius: 13).fill()
    }
    NSGraphicsContext.restoreGraphicsState()
    try bitmap.representation(using: .png, properties: [:])!.write(to: url)
}

for size in [16, 32, 128, 256, 512] {
    try render(size, to: directory.appendingPathComponent("icon_\(size)x\(size).png"))
    try render(size * 2, to: directory.appendingPathComponent("icon_\(size)x\(size)@2x.png"))
}
