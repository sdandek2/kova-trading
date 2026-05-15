import SwiftUI

struct TradingBudgetCard: View {
    @State private var budgetStatus: BudgetStatus? = nil
    @State private var isLoading = true
    @State private var isSaving = false
    @State private var saveSuccess = false
    @State private var saveError: String? = nil
    @State private var inputText: String = ""
    @State private var isEditing = false

    private var portfolioValue: Double { budgetStatus?.portfolio_value ?? 0 }
    private var activeBudget: Double? { budgetStatus?.trading_budget }
    private var usingFull: Bool { budgetStatus?.using_full_portfolio ?? true }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            // ── Header ──
            HStack(spacing: 10) {
                ZStack {
                    Circle().fill(Color.green.opacity(0.12)).frame(width: 36, height: 36)
                    Image(systemName: "dollarsign.circle.fill")
                        .foregroundStyle(.green).font(.system(size: 16))
                }
                VStack(alignment: .leading, spacing: 2) {
                    Text("Trading Budget")
                        .font(.headline).foregroundStyle(.primary)
                    Text(usingFull ? "Using full portfolio" : "Capped at \(formatCurrency(activeBudget ?? 0))")
                        .font(.caption).foregroundStyle(usingFull ? Color.secondary : Color.orange)
                }
                Spacer()
                if isSaving {
                    ProgressView().scaleEffect(0.8)
                } else if saveSuccess {
                    Image(systemName: "checkmark.circle.fill")
                        .foregroundStyle(.green)
                        .transition(.scale.combined(with: .opacity))
                }
            }
            .padding()

            Divider().padding(.horizontal)

            if isLoading {
                ProgressView().frame(maxWidth: .infinity).padding()
            } else {
                VStack(spacing: 12) {
                    // ── Portfolio value row ──
                    HStack {
                        Text("Your account")
                            .font(.subheadline).foregroundStyle(.secondary)
                        Spacer()
                        Text(formatCurrency(portfolioValue))
                            .font(.subheadline).fontWeight(.semibold)
                            .foregroundStyle(.primary)
                    }
                    .padding(.horizontal)

                    // ── Active budget row ──
                    HStack {
                        Text("Trading with")
                            .font(.subheadline).foregroundStyle(.secondary)
                        Spacer()
                        if usingFull {
                            Text("Full portfolio")
                                .font(.subheadline).fontWeight(.semibold)
                                .foregroundStyle(.green)
                        } else {
                            Text(formatCurrency(activeBudget ?? 0))
                                .font(.subheadline).fontWeight(.semibold)
                                .foregroundStyle(.orange)
                        }
                    }
                    .padding(.horizontal)

                    Divider().padding(.horizontal)

                    // ── Input row ──
                    if isEditing {
                        HStack(spacing: 8) {
                            Text("$")
                                .font(.subheadline).foregroundStyle(.secondary)
                            TextField("e.g. 2000", text: $inputText)
                                .keyboardType(.decimalPad)
                                .font(.subheadline)
                                .textFieldStyle(.roundedBorder)
                            Button("Set") {
                                applyBudget()
                            }
                            .buttonStyle(.borderedProminent)
                            .tint(.orange)
                            .disabled(inputText.isEmpty)

                            Button("Cancel") {
                                isEditing = false
                                inputText = ""
                            }
                            .foregroundStyle(.secondary)
                        }
                        .padding(.horizontal)
                    } else {
                        HStack(spacing: 8) {
                            Button {
                                inputText = activeBudget.map { String(format: "%.0f", $0) } ?? ""
                                isEditing = true
                            } label: {
                                Label("Set Budget", systemImage: "pencil")
                                    .frame(maxWidth: .infinity)
                            }
                            .buttonStyle(.bordered)
                            .tint(.orange)

                            if !usingFull {
                                Button {
                                    clearBudget()
                                } label: {
                                    Label("Use Full", systemImage: "arrow.uturn.left")
                                        .frame(maxWidth: .infinity)
                                }
                                .buttonStyle(.bordered)
                                .tint(.green)
                            }
                        }
                        .padding(.horizontal)
                    }

                    if let err = saveError {
                        Text(err)
                            .font(.caption).foregroundStyle(.red)
                            .padding(.horizontal)
                    }
                }
                .padding(.vertical, 12)
            }
        }
        .background(RoundedRectangle(cornerRadius: 16).fill(Color(.systemGray6)))
        .task { await load() }
    }

    private func load() async {
        isLoading = true
        if let status = try? await APIService.shared.getTradingBudget() {
            budgetStatus = status
        }
        isLoading = false
    }

    private func applyBudget() {
        guard let amount = Double(inputText), amount > 0 else {
            saveError = "Enter a valid amount"
            return
        }
        isSaving = true
        saveError = nil
        isEditing = false
        Task {
            do {
                try await APIService.shared.setTradingBudget(amount)
                if let status = try? await APIService.shared.getTradingBudget() {
                    await MainActor.run { budgetStatus = status }
                }
                await MainActor.run {
                    isSaving = false
                    saveSuccess = true
                    inputText = ""
                }
                try? await Task.sleep(nanoseconds: 2_000_000_000)
                await MainActor.run { saveSuccess = false }
            } catch {
                await MainActor.run {
                    isSaving = false
                    saveError = "Save failed: \(error.localizedDescription)"
                }
            }
        }
    }

    private func clearBudget() {
        isSaving = true
        saveError = nil
        Task {
            do {
                try await APIService.shared.setTradingBudget(nil)
                if let status = try? await APIService.shared.getTradingBudget() {
                    await MainActor.run { budgetStatus = status }
                }
                await MainActor.run {
                    isSaving = false
                    saveSuccess = true
                }
                try? await Task.sleep(nanoseconds: 2_000_000_000)
                await MainActor.run { saveSuccess = false }
            } catch {
                await MainActor.run {
                    isSaving = false
                    saveError = "Clear failed: \(error.localizedDescription)"
                }
            }
        }
    }

    private func formatCurrency(_ value: Double) -> String {
        let formatter = NumberFormatter()
        formatter.numberStyle = .currency
        formatter.maximumFractionDigits = 0
        return formatter.string(from: NSNumber(value: value)) ?? "$\(Int(value))"
    }
}
