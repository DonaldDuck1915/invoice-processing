# Demo invoices

Upload in numeric order (some scenarios depend on earlier ones).

| File | Expected | Scenario |
|---|---|---|
| 01_happy_path_digital.pdf | APPROVED | Clean digital invoice, explicit PO, all within tolerance. |
| 02_happy_path_scanned.pdf | APPROVED | Scanned image PDF; OCR path. |
| 03_ec1_split_a_60_chairs.pdf | APPROVED | First of two split invoices (60/100). |
| 04_ec1_split_b_40_chairs.pdf | APPROVED | Second split invoice (40/100) - PO now fully consumed. |
| 05_ec1_split_c_overbill_5_chairs.pdf | REJECTED | Cumulative 105 > PO 100. |
| 06_ec2_original_eur.pdf | APPROVED | Original EUR invoice. |
| 07_ec2_resend_scanned_ocr_number.pdf | MANUAL_REVIEW | Same invoice re-sent: new filename, different layout, scanned, invoice no. misread (0 -> 8). |
| 08_ec2_recurring_jan.pdf | APPROVED | Monthly service invoice. |
| 09_ec2_recurring_feb_same_amount.pdf | APPROVED | Same vendor + same amount but a genuinely new invoice - must NOT be flagged. |
| 10_ec3_price_8pct_over.pdf | MANUAL_REVIEW | Unit price 1,080 vs PO 1,000 = 8% > 5% tolerance. |
| 11_ec3_price_3pct_within.pdf | APPROVED | Unit price 1,030 = 3% - within tolerance. |
| 12_ec4_no_po_ambiguous.pdf | MANUAL_REVIEW | No PO number; 'office supplies' for INR 50,000 fits PO-101 and PO-103 equally. |
| 13_ec4_no_po_clear_lines.pdf | APPROVED* | No PO number but line items/codes clearly point to PO-101 (*review if AI is off). |
| 14_reject_cancelled_po.pdf | REJECTED | Invoice against a cancelled PO. |
| 15_reject_po_of_other_vendor.pdf | REJECTED | Cites a PO that belongs to a different vendor. |
| 16_review_blocked_vendor.pdf | MANUAL_REVIEW | Vendor is BLOCKED - flagged for review. |
| 17_review_pending_vendor.pdf | MANUAL_REVIEW | Vendor approval PENDING - flagged for review. |
| 18_review_currency_mismatch.pdf | MANUAL_REVIEW | USD invoice against an INR PO. |
| 19_review_missing_number_math_error.pdf | MANUAL_REVIEW | No invoice number; line 2 total (652.50) != 100 x 6.25. |
| 20_review_low_quality_scan.pdf | MANUAL_REVIEW | Barely legible scan - low OCR confidence. |
| 23_ec5_tax_inclusive_prices.pdf | APPROVED | Prices include 18% GST (2,950 per stand vs PO 2,500 net). Net values are derived before matching, so it is not mistaken for an 18% price variance. |
| 24_credit_note_return_5_chairs.pdf | APPROVED | Credit note for 5 returned chairs against 03: reduces that invoice by 29,500 and gives 5 chairs back to PO-2001. |
| 25_debit_note_freight.pdf | MANUAL_REVIEW | Vendor debit note adding freight to 01: fits PO-2005's value tolerance but needs the buyer's OK. |
| 26_limit_above_manager.pdf | MANUAL_REVIEW | 5,52,240 - above the 5,00,000 auto-approval limit; only a manager can approve. |
| 27_limit_just_under_a.pdf | APPROVED | 4,77,900 - just under the limit: approved, with a 'just under limit' flag. |
| 28_limit_just_under_b_split.pdf | MANUAL_REVIEW | Second just-under-limit invoice on the same PO 6 days later - possible split to avoid manager approval. |
| 29_fraud_bank_details_changed.pdf | MANUAL_REVIEW | Asks for payment to a bank account that differs from the vendor master. |
| 30_fraud_invoice_before_po.pdf | MANUAL_REVIEW | Invoice dated 25 Aug, before PO-103 was raised (3 Sep) - retro PO. Bank details match the master. |
| 31_einvoice_valid_qr.pdf | APPROVED | Valid e-invoice: IRN and signed QR match the printed invoice. TDS 194C 2% withheld. |
| 32_einvoice_missing_irn.pdf | MANUAL_REVIEW | Vendor must e-invoice but sent a plain invoice - no IRN, no QR. |
| 33_einvoice_qr_mismatch.pdf | MANUAL_REVIEW | Printed total 2,24,200 but the signed QR says 1,12,100 - the PDF was altered after the IRN was issued. |
| 21_error_corrupt.pdf | Error | Truncated / corrupt PDF |
| 22_error_not_a_pdf.pdf | Error | Not a PDF at all |

Re-uploading `06_ec2_original_eur.pdf` a second time demonstrates a CONFIRMED duplicate (REJECTED).
