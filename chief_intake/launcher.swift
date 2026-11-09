// Chief Intake launcher.
//
// macOS grants Full Disk Access and Notes automation to the process that launchd
// starts. This small signed app is that process, so the grant has a stable name
// ("Chief Intake") and survives Python upgrades. It runs the command sealed in its
// own Info.plist as a CHILD process: children inherit this app's permissions.
// exec'ing Python instead would replace this identity and lose the grant.
//
// The only accepted argument is --preflight, so the app cannot be used to run
// arbitrary commands with its permissions.
import Foundation

func fail(_ message: String, _ code: Int32) -> Never {
    FileHandle.standardError.write(Data("Chief Intake launcher: \(message)\n".utf8))
    exit(code)
}

guard let command = Bundle.main.object(forInfoDictionaryKey: "ChiefIntakeCommand") as? [String],
      let executable = command.first else {
    fail("ChiefIntakeCommand is missing from Info.plist", 78)
}

let mode: [String]
switch Array(CommandLine.arguments.dropFirst()) {
case []: mode = ["--trigger", "launchd"]
case ["--preflight"]: mode = ["--preflight"]
default: fail("unsupported arguments; only --preflight is accepted", 64)
}

let child = Process()
child.executableURL = URL(fileURLWithPath: executable)
child.arguments = Array(command.dropFirst()) + mode

// launchd stops jobs with SIGTERM; pass it on so the child can clean up its lock.
signal(SIGTERM, SIG_IGN)
let terminate = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
terminate.setEventHandler { child.terminate() }
terminate.resume()

do {
    try child.run()
} catch {
    fail("cannot start \(executable): \(error)", 71)
}
child.waitUntilExit()
exit(child.terminationReason == .uncaughtSignal ? 128 + child.terminationStatus : child.terminationStatus)
