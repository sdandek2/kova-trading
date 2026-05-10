import SwiftUI

struct BotControlView: View {
    @ObservedObject var vm: TradingViewModel

    var body: some View {
        VStack(spacing: 16) {
            HStack {
                VStack(alignment: .leading, spacing: 4) {
                    Text("AI Trading Bot")
                        .font(.headline)
                    Text(vm.status?.isRunning == true ? "Running · analyzing markets every 10 min" : "Stopped · no trades will be placed")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                Spacer()
                Toggle("", isOn: Binding(
                    get: { vm.status?.isRunning ?? false },
                    set: { _ in Task { await vm.toggleBot() } }
                ))
                .labelsHidden()
            }

            if vm.status?.isRunning == true, vm.countdown > 0 {
                HStack {
                    Image(systemName: "clock")
                        .foregroundStyle(.secondary)
                    Text("Next analysis in \(vm.countdown)s")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    Spacer()
                }
            }
        }
        .padding()
        .background(RoundedRectangle(cornerRadius: 16).fill(Color(.systemGray6)))
    }
}
