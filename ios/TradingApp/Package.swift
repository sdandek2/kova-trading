// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "TradingApp",
    platforms: [.iOS(.v16)],
    targets: [
        .target(
            name: "TradingApp",
            path: "Sources",
            resources: [.process("Assets.xcassets")]
        )
    ]
)
