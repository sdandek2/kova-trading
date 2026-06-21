import SwiftUI

struct OrdersView: View {
    @StateObject private var vm = OrdersViewModel()

    var body: some View {
        NavigationStack {
            Group {
                if vm.isLoading && vm.orders.isEmpty {
                    LoadingView()
                } else if let error = vm.errorMessage, vm.orders.isEmpty {
                    ErrorView(message: error) { await vm.load() }
                } else if vm.orders.isEmpty {
                    VStack(spacing: 16) {
                        ZStack {
                            Circle()
                                .fill(LakshmiTheme.gold.opacity(0.10))
                                .frame(width: 80, height: 80)
                            Image(systemName: "tray.fill")
                                .font(.system(size: 30))
                                .foregroundStyle(LakshmiTheme.gold.opacity(0.55))
                        }
                        Text("No Orders Yet")
                            .font(.title3.weight(.bold))
                        Text("Orders placed by the AI will appear here.")
                            .font(.subheadline)
                            .foregroundStyle(.secondary)
                            .multilineTextAlignment(.center)
                    }
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                    .padding(40)
                } else {
                    List(vm.orders) { order in
                        OrderRowView(order: order)
                            .swipeActions(edge: .trailing, allowsFullSwipe: false) {
                                if ["accepted", "pending_new", "new", "partially_filled"].contains(order.status) {
                                    Button(role: .destructive) {
                                        Task { await vm.cancelOrder(order) }
                                    } label: {
                                        Label("Cancel", systemImage: "xmark.circle")
                                    }
                                }
                            }
                            .listRowBackground(Color.clear)
                            .listRowSeparator(.hidden)
                            .listRowInsets(EdgeInsets(top: 4, leading: 16, bottom: 4, trailing: 16))
                    }
                    .listStyle(.plain)
                    .scrollContentBackground(.hidden)
                    .background(LakshmiTheme.pageBackground)
                    .refreshable { await vm.load() }
                }
            }
            .navigationTitle("Orders")
        }
        .task { await vm.load() }
    }
}
