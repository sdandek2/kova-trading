import SwiftUI

struct DashboardView: View {
    @StateObject private var vm = DashboardViewModel()
    @State private var showTradeSheet = false

    var body: some View {
        NavigationStack {
            Group {
                if vm.isLoading && vm.account == nil {
                    LoadingView()
                } else if let error = vm.errorMessage, vm.account == nil {
                    ErrorView(message: error) { await vm.load() }
                } else {
                    ScrollView {
                        VStack(spacing: 20) {
                            if let account = vm.account {
                                AccountCardView(account: account)
                            }

                            PortfolioChartView(points: vm.portfolioHistory)
                                .padding(.horizontal)

                            VStack(alignment: .leading, spacing: 0) {
                                Text("Positions")
                                    .font(.title3)
                                    .fontWeight(.semibold)
                                    .padding(.horizontal)
                                    .padding(.bottom, 8)

                                if vm.positions.isEmpty {
                                    Text("No open positions")
                                        .foregroundStyle(.secondary)
                                        .padding(.horizontal)
                                        .padding(.vertical, 20)
                                } else {
                                    LazyVStack(spacing: 0) {
                                        ForEach(vm.positions) { position in
                                            PositionRowView(position: position)
                                                .padding(.horizontal)
                                            if position.id != vm.positions.last?.id {
                                                Divider().padding(.leading)
                                            }
                                        }
                                    }
                                    .background(Color(.systemGray6))
                                    .clipShape(RoundedRectangle(cornerRadius: 12))
                                    .padding(.horizontal)
                                }
                            }
                        }
                        .padding(.vertical)
                    }
                    .refreshable { await vm.load() }
                }
            }
            .navigationTitle("Dashboard")
            .toolbar {
                ToolbarItem(placement: .primaryAction) {
                    Button {
                        showTradeSheet = true
                    } label: {
                        Label("Trade", systemImage: "arrow.left.arrow.right.circle.fill")
                            .labelStyle(.iconOnly)
                            .font(.title3)
                    }
                }
            }
            .sheet(isPresented: $showTradeSheet) {
                TradeSheet()
            }
        }
        .task { await vm.load() }
    }
}
