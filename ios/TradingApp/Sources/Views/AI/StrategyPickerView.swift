import SwiftUI

struct StrategyPickerView: View {
    @State private var currentStrategy = "aggressive"
    @State private var isUpdating = false

    let strategies = [
        ("conservative", "Conservative", "shield.fill", "Small positions, tight stops, high-confidence only", Color.blue),
        ("balanced", "Balanced", "scale.3d", "Balance growth and risk, standard sizing", Color.green),
        ("aggressive", "Aggressive", "bolt.fill", "Larger positions, trade more frequently", Color.orange),
    ]

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Trading Strategy")
                .font(.headline)

            ForEach(strategies, id: \.0) { key, name, icon, description, color in
                Button {
                    Task { await selectStrategy(key) }
                } label: {
                    HStack(spacing: 12) {
                        Image(systemName: icon)
                            .font(.title2)
                            .foregroundStyle(color)
                            .frame(width: 36)

                        VStack(alignment: .leading, spacing: 2) {
                            Text(name)
                                .font(.subheadline)
                                .fontWeight(.semibold)
                                .foregroundStyle(.primary)
                            Text(description)
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }

                        Spacer()

                        if currentStrategy == key {
                            Image(systemName: "checkmark.circle.fill")
                                .foregroundStyle(color)
                        }
                    }
                    .padding(12)
                    .background(
                        RoundedRectangle(cornerRadius: 12)
                            .fill(currentStrategy == key ? color.opacity(0.1) : Color(.systemBackground))
                    )
                    .overlay(
                        RoundedRectangle(cornerRadius: 12)
                            .strokeBorder(currentStrategy == key ? color : Color.clear, lineWidth: 1.5)
                    )
                }
                .disabled(isUpdating)
            }
        }
        .padding()
        .background(RoundedRectangle(cornerRadius: 16).fill(Color(.systemGray6)))
        .onAppear {
            Task { await fetchCurrentStrategy() }
        }
    }

    private func fetchCurrentStrategy() async {
        if let strategy = try? await APIService.shared.getStrategy() {
            currentStrategy = strategy
        }
    }

    private func selectStrategy(_ key: String) async {
        isUpdating = true
        do {
            try await APIService.shared.setStrategy(key)
            currentStrategy = key
        } catch {}
        isUpdating = false
    }
}
