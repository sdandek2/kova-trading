import SwiftUI

struct TradingFloorView: View {
    @State private var settings: RiskSettings = .defaults
    @State private var isSaving = false
    @State private var savedFeedback = false
    @State private var isLoading = true

    // Hours available for afternoon pressure: 12 PM – 3 PM EST
    private let availableHours = [12, 13, 14, 15]
    private func hourLabel(_ h: Int) -> String {
        let suffix = h >= 12 ? "PM" : "AM"
        let display = h > 12 ? h - 12 : h
        return "\(display):00 \(suffix)"
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Daily Trade Target")
                .font(.headline)

            if isLoading {
                ProgressView()
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 8)
            } else {
                // Min trades per day
                VStack(alignment: .leading, spacing: 6) {
                    HStack {
                        VStack(alignment: .leading, spacing: 2) {
                            Text("Min trades per day")
                                .font(.subheadline)
                            Text("Triggers afternoon push if below this")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                        Spacer()
                        Stepper("\(settings.min_daily_trades)",
                                value: Binding(
                                    get: { settings.min_daily_trades },
                                    set: { settings.min_daily_trades = max(1, min(10, $0)) }
                                ),
                                in: 1...10)
                        .labelsHidden()
                        Text("\(settings.min_daily_trades)")
                            .font(.subheadline)
                            .fontWeight(.semibold)
                            .frame(width: 24)
                    }
                }

                Divider()

                // Afternoon pressure hour
                VStack(alignment: .leading, spacing: 6) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Afternoon push time")
                            .font(.subheadline)
                        Text("If min not hit by this time, bot trades more aggressively")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                    Picker("Pressure hour", selection: Binding(
                        get: { settings.afternoon_pressure_hour },
                        set: { settings.afternoon_pressure_hour = $0 }
                    )) {
                        ForEach(availableHours, id: \.self) { h in
                            Text(hourLabel(h)).tag(h)
                        }
                    }
                    .pickerStyle(.segmented)
                }

                // Save button
                Button {
                    Task { await save() }
                } label: {
                    HStack {
                        if isSaving {
                            ProgressView().scaleEffect(0.8)
                        } else if savedFeedback {
                            Image(systemName: "checkmark")
                        }
                        Text(savedFeedback ? "Saved" : "Apply")
                            .fontWeight(.semibold)
                    }
                    .frame(maxWidth: .infinity)
                }
                .buttonStyle(.borderedProminent)
                .disabled(isSaving)
                .tint(savedFeedback ? .green : .blue)
            }
        }
        .padding()
        .background(RoundedRectangle(cornerRadius: 16).fill(Color(.systemGray6)))
        .task { await load() }
    }

    private func load() async {
        isLoading = true
        do {
            settings = try await APIService.shared.getRiskSettings()
        } catch {
            settings = .defaults
        }
        isLoading = false
    }

    private func save() async {
        isSaving = true
        do {
            try await APIService.shared.setRiskSettings(settings)
            withAnimation {
                savedFeedback = true
                isSaving = false
            }
            try? await Task.sleep(nanoseconds: 2_000_000_000)
            withAnimation { savedFeedback = false }
        } catch {
            isSaving = false
        }
    }
}
