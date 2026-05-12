import SwiftUI

struct AIView: View {
    @StateObject private var vm = TradingViewModel()

    var body: some View {
        NavigationStack {
            Group {
                if vm.isLoading && vm.status == nil {
                    LoadingView()
                } else if let error = vm.errorMessage, vm.status == nil {
                    ErrorView(message: error) { await vm.load() }
                } else {
                    ScrollView {
                        VStack(spacing: 16) {
                            PreMarketView()

                            BotControlView(vm: vm)

                            StrategyPickerView()

                            TradingFloorView()

                            WatchlistEditorView()

                            NavigationLink("View Performance Stats") {
                                PerformanceView()
                                    .navigationTitle("Performance")
                            }
                            .buttonStyle(.borderedProminent)
                            .frame(maxWidth: .infinity)

                            if let analysis = vm.analysis {
                                VStack(alignment: .leading, spacing: 12) {
                                    HStack {
                                        Text("Latest AI Decision")
                                            .font(.headline)
                                        Spacer()
                                        if let ts = analysis.timestamp {
                                            Text(ts, style: .relative)
                                                .font(.caption)
                                                .foregroundStyle(.secondary)
                                        }
                                    }

                                    if let action = analysis.lastAction, let symbol = analysis.symbol, action != "hold" {
                                        HStack(spacing: 8) {
                                            Text(action.uppercased())
                                                .font(.caption)
                                                .fontWeight(.bold)
                                                .foregroundStyle(.white)
                                                .padding(.horizontal, 8)
                                                .padding(.vertical, 4)
                                                .background(action == "buy" ? Color.green : Color.red)
                                                .clipShape(Capsule())
                                            Text(symbol)
                                                .font(.subheadline)
                                                .fontWeight(.medium)
                                        }
                                    }

                                    Text(analysis.reasoning)
                                        .font(.subheadline)
                                        .foregroundStyle(.primary)
                                        .lineSpacing(4)
                                }
                                .padding()
                                .background(RoundedRectangle(cornerRadius: 16).fill(Color(.systemGray6)))
                            } else {
                                VStack(spacing: 8) {
                                    Image(systemName: "brain")
                                        .font(.system(size: 32))
                                        .foregroundStyle(.secondary)
                                    Text("No analysis yet")
                                        .font(.subheadline)
                                        .foregroundStyle(.secondary)
                                    Text("Start the bot to see Claude's trading decisions here.")
                                        .font(.caption)
                                        .foregroundStyle(.tertiary)
                                        .multilineTextAlignment(.center)
                                }
                                .frame(maxWidth: .infinity)
                                .padding(.vertical, 40)
                            }
                        }
                        .padding()
                    }
                    .refreshable { await vm.load() }
                }
            }
            .navigationTitle("AI Agent")
        }
        .task { await vm.load() }
    }
}
