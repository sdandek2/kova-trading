import SwiftUI

struct ConnectorHealthView: View {
    @ObservedObject var vm: InsightsViewModel

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text("Connector Health")
                    .font(.headline)
                // Overall status dot
                Circle()
                    .fill(overallStatusColor)
                    .frame(width: 8, height: 8)
                Spacer()
                if vm.isLoadingConnectorHealth {
                    ProgressView().scaleEffect(0.7)
                } else {
                    Button {
                        Task { await vm.loadConnectorHealth() }
                    } label: {
                        Image(systemName: "arrow.clockwise")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
            }
            .padding(.horizontal)

            if vm.connectorHealth.isEmpty && !vm.isLoadingConnectorHealth {
                Text("No connector data yet — starts logging after first trading cycle.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .padding(.horizontal)
            } else {
                VStack(spacing: 6) {
                    ForEach(vm.connectorHealth) { connector in
                        ConnectorHealthRow(connector: connector)
                    }
                }
                .padding(.horizontal)
            }
        }
        .task { await vm.loadConnectorHealth() }
    }

    var overallStatusColor: Color {
        if vm.connectorHealth.contains(where: { $0.failurePct >= 80 }) { return .red }
        if vm.connectorHealth.contains(where: { $0.failurePct >= 40 }) { return .orange }
        if vm.connectorHealth.isEmpty { return .secondary }
        return .green
    }
}

struct ConnectorHealthRow: View {
    let connector: ConnectorHealth

    var statusColor: Color {
        if connector.failurePct >= 80 { return .red }
        if connector.failurePct >= 40 { return .orange }
        return .green
    }

    var statusIcon: String {
        if connector.failurePct >= 80 { return "xmark.circle.fill" }
        if connector.failurePct >= 40 { return "exclamationmark.triangle.fill" }
        return "checkmark.circle.fill"
    }

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: statusIcon)
                .foregroundStyle(statusColor)
                .font(.system(size: 14))

            Text(connector.displayName)
                .font(.subheadline)
                .foregroundStyle(.primary)

            Spacer()

            VStack(alignment: .trailing, spacing: 2) {
                if connector.totalCalls == 0 {
                    Text("No calls")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                } else {
                    Text("\(connector.failurePct)% failed")
                        .font(.caption.bold())
                        .foregroundStyle(statusColor)
                    Text("\(connector.failedCalls)/\(connector.totalCalls) calls")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
            }
        }
        .padding(10)
        .background(Color(.secondarySystemBackground))
        .clipShape(RoundedRectangle(cornerRadius: 10))
    }
}
