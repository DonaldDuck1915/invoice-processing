/** Money arrives from the API as exact decimal strings; format without converting through floats. */
function roundStr(v: string, dp = 2): [string, string, boolean] {
  let neg = v.startsWith("-");
  let s = neg ? v.slice(1) : v;
  let [int, frac = ""] = s.split(".");
  if (frac.length > dp) {
    const roundUp = Number(frac[dp]) >= 5;
    frac = frac.slice(0, dp);
    if (roundUp) {
      const digits = (int + frac).split("").map(Number);
      let i = digits.length - 1;
      while (i >= 0) { if (digits[i] === 9) { digits[i] = 0; i--; } else { digits[i]++; break; } }
      let joined = digits.join("");
      if (i < 0) joined = "1" + joined;
      int = joined.slice(0, joined.length - dp);
      frac = joined.slice(joined.length - dp);
    }
  }
  frac = frac.padEnd(dp, "0");
  int = int.replace(/^0+(?=\d)/, "") || "0";
  if (/^0+$/.test(int + frac)) neg = false;
  return [int, frac, neg];
}

function group(int: string, currency?: string | null) {
  if (currency === "INR" && int.length > 3) {
    const head = int.slice(0, -3).replace(/\B(?=(\d{2})+(?!\d))/g, ",");
    return `${head},${int.slice(-3)}`;
  }
  return int.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}

const SYMBOL: Record<string, string> = { INR: "₹", EUR: "€", USD: "$", GBP: "£" };

export function money(v: string | number | null | undefined, currency?: string | null): string {
  if (v === null || v === undefined || v === "") return "—";
  const [int, frac, neg] = roundStr(String(v));
  const sym = currency ? SYMBOL[currency] ?? `${currency} ` : "";   // other currencies show their code, e.g. "AED 1,200.00"
  return `${neg ? "-" : ""}${sym}${group(int, currency)}.${frac}`;
}

export function num(v: string | number | null | undefined): string {
  if (v === null || v === undefined || v === "") return "—";
  const s = String(v);
  if (!s.includes(".")) return group(s);
  const [int, frac] = s.split(".");
  const trimmed = frac.replace(/0+$/, "");
  return trimmed ? `${group(int)}.${trimmed}` : group(int);
}

export function pct(v: string | number | null | undefined): string {
  if (v === null || v === undefined || v === "") return "—";
  const s = String(v);
  return `${s.startsWith("-") ? "" : "+"}${s}%`;
}

export function dateTime(v?: string | null) {
  if (!v) return "—";
  const d = new Date(v);
  return d.toLocaleString(undefined, { day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" });
}

export function dateOnly(v?: string | null) {
  if (!v) return "—";
  const [y, m, d] = v.slice(0, 10).split("-");
  const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  return `${d} ${months[Number(m) - 1]} ${y}`;
}

export const FIELD_LABELS: Record<string, string> = {
  vendor_name: "Vendor name", vendor_tax_id: "Vendor tax ID (GSTIN/VAT/EIN)", vendor_address: "Vendor address",
  invoice_number: "Invoice number", invoice_date: "Invoice date", due_date: "Due date", po_number: "PO reference",
  currency: "Currency", subtotal: "Subtotal", taxable_amount: "Taxable amount", tax_amount: "Tax amount",
  discount: "Discount", shipping: "Shipping / freight", grand_total: "Grand total",
  document_type: "Document type (invoice / credit note / debit note)", original_invoice_number: "Original invoice (for a note)",
  bank_account: "Bank account / IBAN", bank_ifsc: "IFSC / SWIFT", irn: "E-invoice IRN",
};
export const MONEY_FIELDS = new Set(["subtotal", "taxable_amount", "tax_amount", "discount", "shipping", "grand_total"]);
