import json

import frappe
from frappe import _
from frappe.model.utils import get_fetch_values
from erpnext.controllers.accounts_controller import get_taxes_and_charges

from india_compliance.gst_india.constants import STATE_NUMBERS
from india_compliance.gst_india.utils import get_gst_accounts, get_place_of_supply


def set_place_of_supply(doc, method=None):
    doc.place_of_supply = get_place_of_supply(doc, doc.doctype)


def validate_hsn_code(doc, method=None):
    validate_hsn_code, min_hsn_digits = frappe.get_cached_value(
        "GST Settings",
        "GST Settings",
        ("validate_hsn_code", "min_hsn_digits"),
    )

    if not validate_hsn_code:
        return

    missing_hsn = []
    invalid_hsn = []
    min_hsn_digits = int(min_hsn_digits)

    for item in doc.items:
        if not (hsn_code := item.get("gst_hsn_code")):
            if doc._action == "submit":
                invalid_hsn.append(str(item.idx))
            else:
                missing_hsn.append(str(item.idx))

        elif len(hsn_code) < min_hsn_digits:
            invalid_hsn.append(str(item.idx))

    if doc._action == "submit":
        if not invalid_hsn:
            return

        frappe.throw(
            _(
                "Please enter a valid HSN/SAC code for the following row numbers:"
                " <br>{0}"
            ).format(frappe.bold(", ".join(invalid_hsn)))
        )

    if missing_hsn:
        frappe.msgprint(
            _(
                "Please enter HSN/SAC code for the following row numbers: <br>{0}"
            ).format(frappe.bold(", ".join(missing_hsn)))
        )

    if invalid_hsn:
        frappe.msgprint(
            _(
                "HSN/SAC code should be at least {0} digits long for the following"
                " row numbers: <br>{1}"
            ).format(min_hsn_digits, frappe.bold(", ".join(invalid_hsn)))
        )


def get_itemised_tax_breakup_header(item_doctype, tax_accounts):
    hsn_wise_in_gst_settings = frappe.db.get_single_value(
        "GST Settings", "hsn_wise_tax_breakup"
    )
    if (
        frappe.get_meta(item_doctype).has_field("gst_hsn_code")
        and hsn_wise_in_gst_settings
    ):
        return [_("HSN/SAC"), _("Taxable Amount")] + tax_accounts
    else:
        return [_("Item"), _("Taxable Amount")] + tax_accounts


@frappe.whitelist()
def get_regional_round_off_accounts(company, account_list):
    country = frappe.get_cached_value("Company", company, "country")

    if country != "India":
        return

    if isinstance(account_list, str):
        account_list = json.loads(account_list)

    if not frappe.db.get_single_value("GST Settings", "round_off_gst_values"):
        return

    gst_accounts = get_gst_accounts(company)

    gst_account_list = []
    for account in ["cgst_account", "sgst_account", "igst_account"]:
        if account in gst_accounts:
            gst_account_list += gst_accounts.get(account)

    account_list.extend(gst_account_list)

    return account_list


@frappe.whitelist()
def get_regional_address_details(party_details, doctype, company):
    if isinstance(party_details, str):
        party_details = json.loads(party_details)
        party_details = frappe._dict(party_details)

    update_party_details(party_details, doctype)

    party_details.place_of_supply = get_place_of_supply(party_details, doctype)

    if is_internal_transfer(party_details, doctype):
        party_details.taxes_and_charges = ""
        party_details.taxes = []
        return party_details

    if doctype in ("Sales Invoice", "Delivery Note", "Sales Order"):
        master_doctype = "Sales Taxes and Charges Template"
        tax_template_by_category = get_tax_template_based_on_category(
            master_doctype, company, party_details
        )

    elif doctype in ("Purchase Invoice", "Purchase Order", "Purchase Receipt"):
        master_doctype = "Purchase Taxes and Charges Template"
        tax_template_by_category = get_tax_template_based_on_category(
            master_doctype, company, party_details
        )

    if tax_template_by_category:
        party_details["taxes_and_charges"] = tax_template_by_category
        return party_details

    if not party_details.place_of_supply:
        return party_details
    if not party_details.company_gstin:
        return party_details

    if (
        doctype in ("Sales Invoice", "Delivery Note", "Sales Order")
        and party_details.company_gstin
        and party_details.company_gstin[:2] != party_details.place_of_supply[:2]
    ) or (
        doctype in ("Purchase Invoice", "Purchase Order", "Purchase Receipt")
        and party_details.supplier_gstin
        and party_details.supplier_gstin[:2] != party_details.place_of_supply[:2]
    ):
        default_tax = get_tax_template(
            master_doctype, company, 1, party_details.company_gstin[:2]
        )
    else:
        default_tax = get_tax_template(
            master_doctype, company, 0, party_details.company_gstin[:2]
        )

    if not default_tax:
        return party_details

    party_details["taxes_and_charges"] = default_tax
    party_details.taxes = get_taxes_and_charges(master_doctype, default_tax)

    return party_details


def set_item_tax_from_hsn_code(item):
    if not item.taxes and item.gst_hsn_code:
        hsn_doc = frappe.get_doc("GST HSN Code", item.gst_hsn_code)

        for tax in hsn_doc.taxes:
            item.append(
                "taxes",
                {
                    "item_tax_template": tax.item_tax_template,
                    "tax_category": tax.tax_category,
                    "valid_from": tax.valid_from,
                },
            )


def update_party_details(party_details, doctype):
    for address_field in [
        "shipping_address",
        "company_address",
        "supplier_address",
        "shipping_address_name",
        "customer_address",
    ]:
        if party_details.get(address_field):
            party_details.update(
                get_fetch_values(
                    doctype, address_field, party_details.get(address_field)
                )
            )


def is_internal_transfer(party_details, doctype):
    if doctype in ("Sales Invoice", "Delivery Note", "Sales Order"):
        destination_gstin = party_details.company_gstin
    elif doctype in ("Purchase Invoice", "Purchase Order", "Purchase Receipt"):
        destination_gstin = party_details.supplier_gstin

    if not destination_gstin or party_details.gstin:
        return False

    if party_details.gstin == destination_gstin:
        return True
    else:
        False


def get_tax_template_based_on_category(master_doctype, company, party_details):
    if not party_details.get("tax_category"):
        return

    default_tax = frappe.db.get_value(
        master_doctype,
        {"company": company, "tax_category": party_details.get("tax_category")},
        "name",
    )

    return default_tax


def get_tax_template(master_doctype, company, is_inter_state, state_code):
    tax_categories = frappe.get_all(
        "Tax Category",
        fields=["name", "is_inter_state", "gst_state"],
        filters={"is_inter_state": is_inter_state, "is_reverse_charge": 0},
    )

    default_tax = ""

    for tax_category in tax_categories:
        if STATE_NUMBERS.get(tax_category.gst_state) == state_code or (
            not default_tax and not tax_category.gst_state
        ):
            default_tax = frappe.db.get_value(
                master_doctype,
                {"company": company, "disabled": 0, "tax_category": tax_category.name},
                "name",
            )
    return default_tax