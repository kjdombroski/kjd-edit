// kjd-audio — captures iZotope RX 12's audio (or all Mac audio if RX isn't running) with ScreenCaptureKit
// and writes it to stdout as interleaved 16-bit little-endian PCM, 48 kHz stereo, for KJD RX Bridge to stream
// to KJD Edit on the iPad. Monitoring only — nothing is written to disk.
// Build: swiftc -O kjd_audio.swift -o kjd-audio      (macOS 13+)
import Foundation
import ScreenCaptureKit
import CoreMedia
import AVFoundation

setvbuf(stdout, nil, _IONBF, 0)
signal(SIGPIPE, SIG_DFL)  // exit quietly when the bridge stops reading

@inline(__always) func s16(_ x: Float) -> Int16 {
    guard x.isFinite else { return 0 }
    return Int16(max(-1, min(1, x)) * 32767).littleEndian
}

final class Capture: NSObject, SCStreamOutput, SCStreamDelegate {
    var stream: SCStream?
    let out = FileHandle.standardOutput

    func start(match: String) async throws {
        let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
        guard let display = content.displays.first else { throw NSError(domain: "kjd-audio", code: 1) }
        let filter: SCContentFilter
        if let app = content.applications.first(where: { $0.applicationName.localizedCaseInsensitiveContains(match) }) {
            FileHandle.standardError.write("capturing app: \(app.applicationName)\n".data(using: .utf8)!)
            filter = SCContentFilter(display: display, including: [app], exceptingWindows: [])
        } else {
            FileHandle.standardError.write("\(match) not running — capturing all Mac audio\n".data(using: .utf8)!)
            filter = SCContentFilter(display: display, excludingApplications: [], exceptingWindows: [])
        }
        let cfg = SCStreamConfiguration()
        cfg.capturesAudio = true
        cfg.sampleRate = 48000
        cfg.channelCount = 2
        cfg.excludesCurrentProcessAudio = true
        cfg.width = 2
        cfg.height = 2
        cfg.minimumFrameInterval = CMTime(value: 1, timescale: 2)
        let s = SCStream(filter: filter, configuration: cfg, delegate: self)
        try s.addStreamOutput(self, type: .audio, sampleHandlerQueue: DispatchQueue(label: "kjd.audio"))
        try s.addStreamOutput(self, type: .screen, sampleHandlerQueue: DispatchQueue(label: "kjd.video"))
        try await s.startCapture()
        stream = s
    }

    func stream(_ stream: SCStream, didOutputSampleBuffer sb: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .audio, sb.isValid else { return }
        try? sb.withAudioBufferList { bufs, _ in
            guard let first = bufs.first, let p0 = first.mData else { return }
            var data: Data
            if bufs.count >= 2, let p1 = bufs[1].mData {  // non-interleaved float32 (the usual case)
                let n = Int(first.mDataByteSize) / 4
                let l = p0.assumingMemoryBound(to: Float.self), r = p1.assumingMemoryBound(to: Float.self)
                data = Data(count: n * 4)
                data.withUnsafeMutableBytes { raw in
                    let o = raw.bindMemory(to: Int16.self)
                    for i in 0..<n {
                        o[2 * i] = s16(l[i])
                        o[2 * i + 1] = s16(r[i])
                    }
                }
            } else {  // interleaved float32 or mono
                let ch = Int(first.mNumberChannels)
                let n = Int(first.mDataByteSize) / 4 / max(ch, 1)
                let f = p0.assumingMemoryBound(to: Float.self)
                data = Data(count: n * 4)
                data.withUnsafeMutableBytes { raw in
                    let o = raw.bindMemory(to: Int16.self)
                    for i in 0..<n {
                        let a = f[i * ch], b = ch > 1 ? f[i * ch + 1] : a
                        o[2 * i] = s16(a)
                        o[2 * i + 1] = s16(b)
                    }
                }
            }
            out.write(data)
        }
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        FileHandle.standardError.write("capture stopped: \(error.localizedDescription)\n".data(using: .utf8)!)
        exit(2)
    }
}

let cap = Capture()
let match = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "RX 12"
Task {
    do { try await cap.start(match: match) } catch {
        FileHandle.standardError.write("cannot capture audio: \(error.localizedDescription) — allow KJD RX Bridge under System Settings › Privacy & Security › Screen & System Audio Recording\n".data(using: .utf8)!)
        exit(1)
    }
}
RunLoop.main.run()
