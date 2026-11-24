// Tap everything the Mac plays, mix to mono, write float32 samples to stdout.
// stderr gets one line, "rate=<hz> ...", once the tap is live, then "drops=<n>" lines if the reader falls behind.
// The tap cleans up and exits on SIGTERM, SIGINT, a closed stdin (the parent is gone, even if it was killed) or a
// closed stdout, so keep stdin open. Usage: systap [seconds]
import CoreAudio
import Foundation

func fail(_ what: String, _ status: OSStatus) -> Never {
    FileHandle.standardError.write("systap: \(what) failed (\(status))\n".data(using: .utf8)!)
    exit(1)
}

let seconds = CommandLine.arguments.count > 1 ? Double(CommandLine.arguments[1]) : nil

let tap = CATapDescription(stereoGlobalTapButExcludeProcesses: [])
tap.uuid = UUID()
tap.name = "lampster-tap"
tap.muteBehavior = .unmuted
tap.isPrivate = true

var tapID = AudioObjectID(kAudioObjectUnknown)
var status = AudioHardwareCreateProcessTap(tap, &tapID)
if status != noErr { fail("create tap", status) }

var formatAddress = AudioObjectPropertyAddress(
    mSelector: kAudioTapPropertyFormat,
    mScope: kAudioObjectPropertyScopeGlobal,
    mElement: kAudioObjectPropertyElementMain)
var format = AudioStreamBasicDescription()
var formatSize = UInt32(MemoryLayout<AudioStreamBasicDescription>.size)
status = AudioObjectGetPropertyData(tapID, &formatAddress, 0, nil, &formatSize, &format)
if status != noErr { fail("read tap format", status) }

let description: [String: Any] = [
    kAudioAggregateDeviceNameKey: "lampster-tap",
    kAudioAggregateDeviceUIDKey: UUID().uuidString,
    kAudioAggregateDeviceIsPrivateKey: true,
    kAudioAggregateDeviceTapAutoStartKey: true,
    kAudioAggregateDeviceTapListKey: [
        [kAudioSubTapUIDKey: tap.uuid.uuidString, kAudioSubTapDriftCompensationKey: true]
    ],
]
var aggregateID = AudioObjectID(kAudioObjectUnknown)
status = AudioHardwareCreateAggregateDevice(description as CFDictionary, &aggregateID)
if status != noErr { fail("create aggregate device", status) }

let channels = Int(format.mChannelsPerFrame)
let interleaved = (format.mFormatFlags & kAudioFormatFlagIsNonInterleaved) == 0
let out = FileHandle.standardOutput
var procID: AudioDeviceIOProcID?
var shuttingDown = false
var drops = 0
var reportedDrops = 0

// Always runs on the main queue. Stops the device first so no callback is in flight while the tap is destroyed.
func shutdown() -> Never {
    if !shuttingDown {
        shuttingDown = true
        AudioDeviceStop(aggregateID, procID)
        if let procID { AudioDeviceDestroyIOProcID(aggregateID, procID) }
        AudioHardwareDestroyAggregateDevice(aggregateID)
        AudioHardwareDestroyProcessTap(tapID)
    }
    exit(0)
}

func requestShutdown() { DispatchQueue.main.async { shutdown() } }

// The audio thread never blocks on the pipe: blocks go to a writer queue, and when the reader is more than about
// 1.4 s behind they are dropped and counted, so a stalled parent cannot wedge the device.
let writer = DispatchQueue(label: "systap.writer")
let slots = DispatchSemaphore(value: 64)

status = AudioDeviceCreateIOProcIDWithBlock(&procID, aggregateID, nil) { _, inData, _, _, _ in
    let buffers = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: inData))
    guard let first = buffers.first, let base = first.mData else { return }
    var mono: [Float]
    if interleaved {
        let frames = Int(first.mDataByteSize) / (MemoryLayout<Float>.size * channels)
        let samples = base.assumingMemoryBound(to: Float.self)
        mono = (0..<frames).map { f in
            (0..<channels).reduce(Float(0)) { $0 + samples[f * channels + $1] } / Float(channels)
        }
    } else {
        let frames = Int(first.mDataByteSize) / MemoryLayout<Float>.size
        mono = [Float](repeating: 0, count: frames)
        for b in buffers {
            guard let data = b.mData else { continue }
            let samples = data.assumingMemoryBound(to: Float.self)
            for f in 0..<frames { mono[f] += samples[f] / Float(buffers.count) }
        }
    }
    let data = mono.withUnsafeBufferPointer { Data(buffer: $0) }
    if slots.wait(timeout: .now()) == .success {
        writer.async {
            do { try out.write(contentsOf: data) } catch { requestShutdown() }
            slots.signal()
        }
    } else {
        drops += 1
    }
}
if status != noErr { fail("create IO proc", status) }

var sources: [DispatchSourceProtocol] = []
for sig in [SIGTERM, SIGINT] {
    signal(sig, SIG_IGN)
    let source = DispatchSource.makeSignalSource(signal: sig, queue: .main)
    source.setEventHandler { shutdown() }
    source.resume()
    sources.append(source)
}

// The parent never writes to stdin: end of file means it is gone.
Thread.detachNewThread {
    while !FileHandle.standardInput.availableData.isEmpty {}
    requestShutdown()
}

let report = DispatchSource.makeTimerSource(queue: .main)
report.schedule(deadline: .now() + 10, repeating: 10)
report.setEventHandler {
    if drops != reportedDrops {
        reportedDrops = drops
        FileHandle.standardError.write("drops=\(drops)\n".data(using: .utf8)!)
    }
}
report.resume()
sources.append(report)

status = AudioDeviceStart(aggregateID, procID)
if status != noErr { fail("start device", status) }

FileHandle.standardError.write("rate=\(Int(format.mSampleRate)) channels=\(channels) interleaved=\(interleaved)\n".data(using: .utf8)!)

if let seconds {
    DispatchQueue.main.asyncAfter(deadline: .now() + seconds) { shutdown() }
}
RunLoop.main.run()
