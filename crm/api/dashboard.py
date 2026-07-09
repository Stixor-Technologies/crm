import json
import re

import frappe
from frappe import _
from frappe.query_builder import Case, DocType
from frappe.query_builder.functions import Avg, Coalesce, Count, Date, DateFormat, IfNull, Sum
from pypika.functions import Function

from crm.fcrm.doctype.crm_dashboard.crm_dashboard import create_default_manager_dashboard
from crm.utils import sales_user_only


# Custom function for TIMESTAMPDIFF (MySQL/MariaDB)
class TimestampDiff(Function):
	def __init__(self, unit, start, end, **kwargs):
		super().__init__("TIMESTAMPDIFF", unit, start, end, **kwargs)


# Row-level dict keys treated as currency amounts by `_apply_currency`. Kept as a small
# closed set so we don't inadvertently convert count/percent fields that happen to be numeric.
_CURRENCY_ROW_KEYS = ("value", "forecasted", "actual")


def _apply_empty_state(data: dict | list | None) -> dict | list | None:
	"""
	Mark axis/donut chart payloads with `isEmpty` when they have no rows to render.

	Number-chart tiles already set their own `isEmpty` at the metric level (they know
	whether the SQL AVG/SUM returned NULL). Axis and donut charts don't — an empty
	`data` list renders as an empty axis or a blank donut, which reads as "broken
	chart" instead of "no qualifying data".

	The frontend uses `isEmpty` to render a "—" placeholder with the emptyReason as a
	tooltip, matching the number-chart empty state.
	"""
	if not isinstance(data, dict):
		return data
	# Respect explicit isEmpty already set by the metric function.
	if data.get("isEmpty") is not None:
		return data
	rows = data.get("data")
	if isinstance(rows, list) and len(rows) == 0:
		data["isEmpty"] = True
		data["emptyReason"] = data.get("emptyReason") or _(
			"No qualifying data in this window"
		)
	return data


def _get_currency_context(target_currency: str | None = None) -> dict:
	"""
	Resolve the currency the dashboard should display in.

	Returns {code, symbol, base_symbol, multiplier}, where `multiplier` converts a
	value already normalized to the base currency (via each deal's stored
	`exchange_rate`) into the target currency, and `base_symbol` is retained so the
	response wrapper can rewrite axis labels of the form `Foo ({base_symbol})`
	without touching unrelated parenthetical suffixes like `(%)` on a rate chart.

	Rate resolution, in order:
	  1. If target == base, multiplier is 1.
	  2. erpnext.setup.utils.get_exchange_rate — the framework helper. It reads
	     `Currency Exchange` rows first, then falls through to the live API configured
	     in `Currency Exchange Settings` (exchangerate.host / frankfurter / etc.),
	     caching each response as a new `Currency Exchange` row. This is the intended
	     path on any site with ERPNext installed and rates configured.
	  3. Derived from the DB: average of `exchange_rate` on deals whose `currency`
	     equals target (that column stores target->base, so base->target = 1/it).
	     Kept as a fallback for sites without ERPNext or with an unreachable rate API.
	  4. Fallback: 1.0, so the chart still renders (values will read as base).
	"""
	base = frappe.db.get_single_value("FCRM Settings", "currency") or "USD"
	target = target_currency or base

	symbol_for = lambda code: frappe.db.get_value("Currency", code, "symbol") or code

	base_symbol = symbol_for(base)

	if target == base:
		return {"code": base, "symbol": base_symbol, "base_symbol": base_symbol, "multiplier": 1.0}

	rate: float | None = None

	try:
		from erpnext.setup.utils import get_exchange_rate

		rate = get_exchange_rate(base, target)
		rate = float(rate) if rate else None
	except Exception:
		# ImportError (ERPNext not installed) or a transient API failure — either way,
		# fall through to the deal-average heuristic below rather than 500-ing the dashboard.
		rate = None

	if not rate:
		row = frappe.db.sql(
			"""
			SELECT AVG(exchange_rate) FROM `tabCRM Deal`
			WHERE currency = %s AND exchange_rate > 0
			""",
			target,
		)
		avg_target_to_base = row[0][0] if row and row[0] else None
		if avg_target_to_base:
			rate = 1.0 / float(avg_target_to_base)

	if not rate:
		rate = 1.0

	return {
		"code": target,
		"symbol": symbol_for(target),
		"base_symbol": base_symbol,
		"multiplier": rate,
	}


def _apply_currency(data: dict | list | None, ctx: dict) -> dict | list | None:
	"""
	Rewrite a chart payload so its currency-bearing values render in the target currency.

	- Replaces `prefix` (number chart) with the target symbol.
	- Multiplies top-level `value` and any row-level `_CURRENCY_ROW_KEYS` by `ctx['multiplier']`.
	- Rewrites any "(<symbol>)" tail in yAxis / y2Axis titles so labels match the shown units.
	No-op when ctx['multiplier'] is 1.0 and the prefix already matches — but we still walk to
	catch the axis-title case where a user picks their own base currency explicitly.
	"""
	if not isinstance(data, dict):
		return data

	multiplier = ctx["multiplier"]
	symbol = ctx["symbol"]

	if "prefix" in data:
		data["prefix"] = symbol

	if isinstance(data.get("value"), int | float):
		data["value"] = data["value"] * multiplier

	# Only rewrite axis titles that carry the *base* currency symbol in parens — this
	# leaves unrelated parenthetical suffixes (like "Win rate (%)") untouched. Charts
	# emit their axis titles as `f"{label} ({get_base_currency_symbol()})"`, so if the
	# symbol isn't in the title, the axis isn't currency-scaled and shouldn't be rewritten.
	base_symbol = ctx.get("base_symbol") or ""
	if base_symbol:
		title_currency_pattern = re.compile(r"\(" + re.escape(base_symbol) + r"\)")
		for axis in ("yAxis", "y2Axis"):
			axis_conf = data.get(axis)
			if isinstance(axis_conf, dict) and isinstance(axis_conf.get("title"), str):
				axis_conf["title"] = title_currency_pattern.sub(f"({symbol})", axis_conf["title"])

	rows = data.get("data")
	if isinstance(rows, list):
		for row in rows:
			if not isinstance(row, dict):
				continue
			for key in _CURRENCY_ROW_KEYS:
				if isinstance(row.get(key), int | float):
					row[key] = row[key] * multiplier

	return data


@frappe.whitelist()
def reset_to_default():
	frappe.only_for("System Manager", True)
	create_default_manager_dashboard(force=True)


@frappe.whitelist()
@sales_user_only
def get_dashboard(
	from_date: str | None = None,
	to_date: str | None = None,
	user: str | None = None,
	currency: str | None = None,
):
	"""
	Get the dashboard data for the CRM dashboard.
	"""

	if not from_date or not to_date:
		from_date = frappe.utils.get_first_day(from_date or frappe.utils.nowdate())
		to_date = frappe.utils.get_last_day(to_date or frappe.utils.nowdate())

	roles = frappe.get_roles(frappe.session.user)
	is_sales_manager = "Sales Manager" in roles or "System Manager" in roles
	is_sales_user = "Sales User" in roles and not is_sales_manager

	if is_sales_user:
		user = frappe.session.user

	dashboard = frappe.db.exists("CRM Dashboard", "Manager Dashboard")

	layout = []

	if not dashboard:
		layout = json.loads(create_default_manager_dashboard())
		frappe.db.commit()
	else:
		layout = json.loads(frappe.db.get_value("CRM Dashboard", "Manager Dashboard", "layout") or "[]")

	currency_ctx = _get_currency_context(currency)

	for l in layout:
		method_name = f"get_{l['name']}"
		if hasattr(frappe.get_attr("crm.api.dashboard"), method_name):
			method = getattr(frappe.get_attr("crm.api.dashboard"), method_name)
			l["data"] = _apply_empty_state(
				_apply_currency(method(from_date, to_date, user), currency_ctx)
			)
		else:
			l["data"] = None

	return layout


@frappe.whitelist()
@sales_user_only
def get_chart(
	name: str,
	type: str,
	from_date: str | None = None,
	to_date: str | None = None,
	user: str | None = None,
	currency: str | None = None,
):
	"""
	Get number chart data for the dashboard.
	"""
	if not from_date or not to_date:
		from_date = frappe.utils.get_first_day(from_date or frappe.utils.nowdate())
		to_date = frappe.utils.get_last_day(to_date or frappe.utils.nowdate())

	roles = frappe.get_roles(frappe.session.user)
	is_sales_manager = "Sales Manager" in roles or "System Manager" in roles
	is_sales_user = "Sales User" in roles and not is_sales_manager

	if is_sales_user:
		user = frappe.session.user

	method_name = f"get_{name}"
	if hasattr(frappe.get_attr("crm.api.dashboard"), method_name):
		method = getattr(frappe.get_attr("crm.api.dashboard"), method_name)
		return _apply_empty_state(
			_apply_currency(method(from_date, to_date, user), _get_currency_context(currency))
		)
	else:
		return {"error": _("Invalid chart name")}


@frappe.whitelist()
@sales_user_only
def list_dashboard_currencies() -> list[dict]:
	"""
	Currencies offered in the dashboard currency picker.

	Returns the base currency plus any other currency that appears on at least one deal.
	Order: base first, then alphabetical.
	"""
	base = frappe.db.get_single_value("FCRM Settings", "currency") or "USD"

	seen = frappe.db.sql_list(
		"SELECT DISTINCT currency FROM `tabCRM Deal` WHERE currency IS NOT NULL AND currency != ''"
	)
	seen = set(seen) | {base}

	rows = []
	for code in sorted(seen, key=lambda c: (c != base, c)):
		symbol = frappe.db.get_value("Currency", code, "symbol") or code
		rows.append({"code": code, "symbol": symbol, "isBase": code == base})
	return rows


def get_total_leads(from_date: str | None = None, to_date: str | None = None, user: str | None = None):
	"""
	Get lead count for the dashboard.
	"""
	diff = frappe.utils.date_diff(to_date, from_date)
	if diff == 0:
		diff = 1

	prev_from_date = frappe.utils.add_days(from_date, -diff)
	to_date_plus_one = frappe.utils.add_days(to_date, 1)

	Lead = DocType("CRM Lead")

	# Build conditions for current period
	current_cond = (Lead.creation >= from_date) & (Lead.creation < to_date_plus_one)
	if user:
		current_cond = current_cond & (Lead.lead_owner == user)

	# Build conditions for previous period
	prev_cond = (Lead.creation >= prev_from_date) & (Lead.creation < from_date)
	if user:
		prev_cond = prev_cond & (Lead.lead_owner == user)

	# Build query with CASE expressions
	query = frappe.qb.from_(Lead).select(
		Count(Case().when(current_cond, Lead.name).else_(None)).as_("current_month_leads"),
		Count(Case().when(prev_cond, Lead.name).else_(None)).as_("prev_month_leads"),
	)

	result = query.run(as_dict=True)

	current_month_leads = result[0].current_month_leads or 0
	prev_month_leads = result[0].prev_month_leads or 0

	delta_in_percentage = (
		(current_month_leads - prev_month_leads) / prev_month_leads * 100 if prev_month_leads else 0
	)

	return {
		"title": _("Total leads"),
		"tooltip": _("Total number of leads"),
		"value": current_month_leads,
		"delta": delta_in_percentage,
		"deltaSuffix": "%",
	}


def get_ongoing_deals(from_date: str | None = None, to_date: str | None = None, user: str | None = None):
	"""
	Get ongoing deal count for the dashboard, and also calculate average deal value for ongoing deals.
	"""
	diff = frappe.utils.date_diff(to_date, from_date)
	if diff == 0:
		diff = 1

	prev_from_date = frappe.utils.add_days(from_date, -diff)
	to_date_plus_one = frappe.utils.add_days(to_date, 1)

	Deal = DocType("CRM Deal")
	Status = DocType("CRM Deal Status")

	# Build conditions for current period
	current_cond = (
		(Deal.creation >= from_date)
		& (Deal.creation < to_date_plus_one)
		& (Status.type.notin(["Won", "Lost"]))
	)
	if user:
		current_cond = current_cond & (Deal.deal_owner == user)

	# Build conditions for previous period
	prev_cond = (
		(Deal.creation >= prev_from_date) & (Deal.creation < from_date) & (Status.type.notin(["Won", "Lost"]))
	)
	if user:
		prev_cond = prev_cond & (Deal.deal_owner == user)

	# Build query with CASE expressions
	query = (
		frappe.qb.from_(Deal)
		.join(Status)
		.on(Deal.status == Status.name)
		.select(
			Count(Case().when(current_cond, Deal.name).else_(None)).as_("current_month_deals"),
			Count(Case().when(prev_cond, Deal.name).else_(None)).as_("prev_month_deals"),
		)
	)

	result = query.run(as_dict=True)

	current_month_deals = result[0].current_month_deals or 0
	prev_month_deals = result[0].prev_month_deals or 0

	delta_in_percentage = (
		(current_month_deals - prev_month_deals) / prev_month_deals * 100 if prev_month_deals else 0
	)

	return {
		"title": _("Ongoing deals"),
		"tooltip": _("Total number of non won/lost deals"),
		"value": current_month_deals,
		"delta": delta_in_percentage,
		"deltaSuffix": "%",
	}


def get_average_ongoing_deal_value(
	from_date: str | None = None, to_date: str | None = None, user: str | None = None
):
	"""
	Get ongoing deal count for the dashboard, and also calculate average deal value for ongoing deals.
	"""
	diff = frappe.utils.date_diff(to_date, from_date)
	if diff == 0:
		diff = 1

	prev_from_date = frappe.utils.add_days(from_date, -diff)
	to_date_plus_one = frappe.utils.add_days(to_date, 1)

	Deal = DocType("CRM Deal")
	Status = DocType("CRM Deal Status")

	# Build conditions for current period
	current_cond = (
		(Deal.creation >= from_date)
		& (Deal.creation < to_date_plus_one)
		& (Status.type.notin(["Won", "Lost"]))
	)
	if user:
		current_cond = current_cond & (Deal.deal_owner == user)

	# Build conditions for previous period
	prev_cond = (
		(Deal.creation >= prev_from_date) & (Deal.creation < from_date) & (Status.type.notin(["Won", "Lost"]))
	)
	if user:
		prev_cond = prev_cond & (Deal.deal_owner == user)

	# Calculate deal value with exchange rate
	deal_value_expr = Deal.deal_value * IfNull(Deal.exchange_rate, 1)

	# Build query with CASE expressions
	query = (
		frappe.qb.from_(Deal)
		.join(Status)
		.on(Deal.status == Status.name)
		.select(
			Avg(Case().when(current_cond, deal_value_expr).else_(None)).as_("current_month_avg_value"),
			Avg(Case().when(prev_cond, deal_value_expr).else_(None)).as_("prev_month_avg_value"),
		)
	)

	result = query.run(as_dict=True)

	current_month_avg_value = result[0].current_month_avg_value
	prev_month_avg_value = result[0].prev_month_avg_value
	is_empty = not current_month_avg_value
	current_month_avg_value = current_month_avg_value or 0
	prev_month_avg_value = prev_month_avg_value or 0

	avg_value_delta = current_month_avg_value - prev_month_avg_value if prev_month_avg_value else 0

	return {
		"title": _("Avg. ongoing deal value"),
		"tooltip": _("Average deal value of non won/lost deals"),
		"value": current_month_avg_value,
		"delta": avg_value_delta,
		"prefix": get_base_currency_symbol(),
		"isEmpty": is_empty,
		"emptyReason": _("No ongoing deals with a value entered"),
	}


def get_won_deals(from_date: str | None = None, to_date: str | None = None, user: str | None = None):
	"""
	Get won deal count for the dashboard, and also calculate average deal value for won deals.
	"""
	diff = frappe.utils.date_diff(to_date, from_date)
	if diff == 0:
		diff = 1

	prev_from_date = frappe.utils.add_days(from_date, -diff)
	to_date_plus_one = frappe.utils.add_days(to_date, 1)

	Deal = DocType("CRM Deal")
	Status = DocType("CRM Deal Status")

	# Build conditions for current period
	current_cond = (
		(Deal.closed_date >= from_date) & (Deal.closed_date < to_date_plus_one) & (Status.type == "Won")
	)
	if user:
		current_cond = current_cond & (Deal.deal_owner == user)

	# Build conditions for previous period
	prev_cond = (Deal.closed_date >= prev_from_date) & (Deal.closed_date < from_date) & (Status.type == "Won")
	if user:
		prev_cond = prev_cond & (Deal.deal_owner == user)

	# Build query with CASE expressions
	query = (
		frappe.qb.from_(Deal)
		.join(Status)
		.on(Deal.status == Status.name)
		.select(
			Count(Case().when(current_cond, Deal.name).else_(None)).as_("current_month_deals"),
			Count(Case().when(prev_cond, Deal.name).else_(None)).as_("prev_month_deals"),
		)
	)

	result = query.run(as_dict=True)

	current_month_deals = result[0].current_month_deals or 0
	prev_month_deals = result[0].prev_month_deals or 0

	delta_in_percentage = (
		(current_month_deals - prev_month_deals) / prev_month_deals * 100 if prev_month_deals else 0
	)

	return {
		"title": _("Won deals"),
		"tooltip": _("Total number of won deals based on its closure date"),
		"value": current_month_deals,
		"delta": delta_in_percentage,
		"deltaSuffix": "%",
	}


def get_average_won_deal_value(
	from_date: str | None = None, to_date: str | None = None, user: str | None = None
):
	"""
	Get won deal count for the dashboard, and also calculate average deal value for won deals.
	"""
	diff = frappe.utils.date_diff(to_date, from_date)
	if diff == 0:
		diff = 1

	prev_from_date = frappe.utils.add_days(from_date, -diff)
	to_date_plus_one = frappe.utils.add_days(to_date, 1)

	Deal = DocType("CRM Deal")
	Status = DocType("CRM Deal Status")

	# Build conditions for current period
	current_cond = (
		(Deal.closed_date >= from_date) & (Deal.closed_date < to_date_plus_one) & (Status.type == "Won")
	)
	if user:
		current_cond = current_cond & (Deal.deal_owner == user)

	# Build conditions for previous period
	prev_cond = (Deal.closed_date >= prev_from_date) & (Deal.closed_date < from_date) & (Status.type == "Won")
	if user:
		prev_cond = prev_cond & (Deal.deal_owner == user)

	# Calculate deal value with exchange rate
	deal_value_expr = Deal.deal_value * IfNull(Deal.exchange_rate, 1)

	# Build query with CASE expressions
	query = (
		frappe.qb.from_(Deal)
		.join(Status)
		.on(Deal.status == Status.name)
		.select(
			Avg(Case().when(current_cond, deal_value_expr).else_(None)).as_("current_month_avg_value"),
			Avg(Case().when(prev_cond, deal_value_expr).else_(None)).as_("prev_month_avg_value"),
		)
	)

	result = query.run(as_dict=True)

	current_month_avg_value = result[0].current_month_avg_value
	prev_month_avg_value = result[0].prev_month_avg_value
	is_empty = not current_month_avg_value
	current_month_avg_value = current_month_avg_value or 0
	prev_month_avg_value = prev_month_avg_value or 0

	avg_value_delta = current_month_avg_value - prev_month_avg_value if prev_month_avg_value else 0

	return {
		"title": _("Avg. won deal value"),
		"tooltip": _("Average deal value of won deals"),
		"value": current_month_avg_value,
		"delta": avg_value_delta,
		"prefix": get_base_currency_symbol(),
		"isEmpty": is_empty,
		"emptyReason": _("No won deals with a deal value entered in this period"),
	}


def get_average_deal_value(from_date: str | None = None, to_date: str | None = None, user: str | None = None):
	"""
	Get average deal value for the dashboard.
	"""
	diff = frappe.utils.date_diff(to_date, from_date)
	if diff == 0:
		diff = 1

	prev_from_date = frappe.utils.add_days(from_date, -diff)
	to_date_plus_one = frappe.utils.add_days(to_date, 1)

	Deal = DocType("CRM Deal")
	Status = DocType("CRM Deal Status")

	# Build conditions for current period
	current_cond = (Deal.creation >= from_date) & (Deal.creation < to_date_plus_one) & (Status.type != "Lost")
	if user:
		current_cond = current_cond & (Deal.deal_owner == user)

	# Build conditions for previous period
	prev_cond = (Deal.creation >= prev_from_date) & (Deal.creation < from_date) & (Status.type != "Lost")
	if user:
		prev_cond = prev_cond & (Deal.deal_owner == user)

	# Calculate deal value with exchange rate
	deal_value_expr = Deal.deal_value * IfNull(Deal.exchange_rate, 1)

	# Build query with CASE expressions
	query = (
		frappe.qb.from_(Deal)
		.join(Status)
		.on(Deal.status == Status.name)
		.select(
			Avg(Case().when(current_cond, deal_value_expr).else_(None)).as_("current_month_avg"),
			Avg(Case().when(prev_cond, deal_value_expr).else_(None)).as_("prev_month_avg"),
		)
	)

	result = query.run(as_dict=True)

	current_month_avg = result[0].current_month_avg
	prev_month_avg = result[0].prev_month_avg
	is_empty = not current_month_avg
	current_month_avg = current_month_avg or 0
	prev_month_avg = prev_month_avg or 0

	delta = current_month_avg - prev_month_avg if prev_month_avg else 0

	return {
		"title": _("Avg. deal value"),
		"tooltip": _("Average deal value of ongoing & won deals"),
		"value": current_month_avg,
		"prefix": get_base_currency_symbol(),
		"delta": delta,
		"deltaSuffix": "%",
		"isEmpty": is_empty,
		"emptyReason": _("No non-lost deals with a value entered in this period"),
	}


def get_average_time_to_close_a_lead(
	from_date: str | None = None, to_date: str | None = None, user: str | None = None
):
	"""
	Get average time from lead creation to deal closure for the dashboard.

	Only counts won deals that were sourced from a linked CRM Lead. Without this
	restriction, deals created directly (without a linked lead) fall through the
	COALESCE and reduce this metric to `get_average_time_to_close_a_deal`, making
	the two indistinguishable.
	"""
	diff = frappe.utils.date_diff(to_date, from_date)
	if diff == 0:
		diff = 1

	prev_from_date = frappe.utils.add_days(from_date, -diff)
	to_date_plus_one = frappe.utils.add_days(to_date, 1)
	prev_to_date = from_date

	Deal = DocType("CRM Deal")
	Status = DocType("CRM Deal Status")
	Lead = DocType("CRM Lead")

	# Base condition: won deal with a resolvable closed_date AND a linked lead.
	base_cond = (
		Deal.closed_date.isnotnull()
		& (Status.type == "Won")
		& Deal.lead.isnotnull()
		& (Deal.lead != "")
	)
	if user:
		base_cond = base_cond & (Deal.deal_owner == user)

	# Current period condition
	current_cond = (Deal.closed_date >= from_date) & (Deal.closed_date < to_date_plus_one)

	# Previous period condition
	prev_cond = (Deal.closed_date >= prev_from_date) & (Deal.closed_date < prev_to_date)

	time_diff = TimestampDiff(
		frappe.qb.terms.LiteralValue("DAY"), Lead.creation, Deal.closed_date
	)

	query = (
		frappe.qb.from_(Deal)
		.join(Status)
		.on(Deal.status == Status.name)
		.join(Lead)  # inner join — the base_cond already requires a lead link
		.on(Deal.lead == Lead.name)
		.where(base_cond)
		.select(
			Avg(Case().when(current_cond, time_diff).else_(None)).as_("current_avg_lead"),
			Avg(Case().when(prev_cond, time_diff).else_(None)).as_("prev_avg_lead"),
		)
	)

	result = query.run(as_dict=True)

	current_avg_lead = result[0].current_avg_lead
	prev_avg_lead = result[0].prev_avg_lead
	is_empty = current_avg_lead is None
	current_avg_lead = current_avg_lead or 0
	prev_avg_lead = prev_avg_lead or 0
	delta_lead = current_avg_lead - prev_avg_lead if prev_avg_lead else 0

	return {
		"title": _("Avg. time to close a lead"),
		"tooltip": _("Average time from lead creation to deal closure (only deals linked to a lead)"),
		"value": current_avg_lead,
		"suffix": " days",
		"delta": delta_lead,
		"deltaSuffix": " days",
		"negativeIsBetter": True,
		"isEmpty": is_empty,
		"emptyReason": _("No lead-sourced won deals in this period"),
	}


def get_average_time_to_close_a_deal(
	from_date: str | None = None, to_date: str | None = None, user: str | None = None
):
	"""
	Get average time to close deals for the dashboard.
	"""
	diff = frappe.utils.date_diff(to_date, from_date)
	if diff == 0:
		diff = 1

	prev_from_date = frappe.utils.add_days(from_date, -diff)
	to_date_plus_one = frappe.utils.add_days(to_date, 1)
	prev_to_date = from_date

	Deal = DocType("CRM Deal")
	Status = DocType("CRM Deal Status")
	Lead = DocType("CRM Lead")

	# Base condition: closed_date is not null and status type is Won
	base_cond = (Deal.closed_date.isnotnull()) & (Status.type == "Won")
	if user:
		base_cond = base_cond & (Deal.deal_owner == user)

	# Current period condition
	current_cond = (Deal.closed_date >= from_date) & (Deal.closed_date < to_date_plus_one)

	# Previous period condition
	prev_cond = (Deal.closed_date >= prev_from_date) & (Deal.closed_date < prev_to_date)

	# Calculate time difference from deal creation to deal closure
	time_diff = TimestampDiff(frappe.qb.terms.LiteralValue("DAY"), Deal.creation, Deal.closed_date)

	# Build query
	query = (
		frappe.qb.from_(Deal)
		.join(Status)
		.on(Deal.status == Status.name)
		.left_join(Lead)
		.on(Deal.lead == Lead.name)
		.where(base_cond)
		.select(
			Avg(Case().when(current_cond, time_diff).else_(None)).as_("current_avg_deal"),
			Avg(Case().when(prev_cond, time_diff).else_(None)).as_("prev_avg_deal"),
		)
	)

	result = query.run(as_dict=True)

	current_avg_deal = result[0].current_avg_deal
	prev_avg_deal = result[0].prev_avg_deal
	is_empty = current_avg_deal is None
	current_avg_deal = current_avg_deal or 0
	prev_avg_deal = prev_avg_deal or 0
	delta_deal = current_avg_deal - prev_avg_deal if prev_avg_deal else 0

	return {
		"title": _("Avg. time to close a deal"),
		"tooltip": _("Average time taken from deal creation to deal closure"),
		"value": current_avg_deal,
		"suffix": " days",
		"delta": delta_deal,
		"deltaSuffix": " days",
		"negativeIsBetter": True,
		"isEmpty": is_empty,
		"emptyReason": _("No won deals in this period"),
	}


def get_sales_trend(from_date: str | None = None, to_date: str | None = None, user: str | None = None):
	"""
	Get sales trend data for the dashboard.
	[
		{ date: new Date('2024-05-01'), leads: 45, deals: 23, won_deals: 12 },
		{ date: new Date('2024-05-02'), leads: 50, deals: 30, won_deals: 15 },
		...
	]
	"""
	if not from_date or not to_date:
		from_date = frappe.utils.get_first_day(from_date or frappe.utils.nowdate())
		to_date = frappe.utils.get_last_day(to_date or frappe.utils.nowdate())

	Lead = DocType("CRM Lead")
	Deal = DocType("CRM Deal")
	Status = DocType("CRM Deal Status")

	# Build leads query
	leads_query = (
		frappe.qb.from_(Lead)
		.select(
			Date(Lead.creation).as_("date"),
			Count("*").as_("leads"),
			frappe.qb.terms.ValueWrapper(0).as_("deals"),
			frappe.qb.terms.ValueWrapper(0).as_("won_deals"),
		)
		.where(Date(Lead.creation).between(from_date, to_date))
	)

	if user:
		leads_query = leads_query.where(Lead.lead_owner == user)

	leads_query = leads_query.groupby(Date(Lead.creation))

	# Build deals query
	deals_query = (
		frappe.qb.from_(Deal)
		.join(Status)
		.on(Deal.status == Status.name)
		.select(
			Date(Deal.creation).as_("date"),
			frappe.qb.terms.ValueWrapper(0).as_("leads"),
			Count("*").as_("deals"),
			Sum(Case().when(Status.type == "Won", 1).else_(0)).as_("won_deals"),
		)
		.where(Date(Deal.creation).between(from_date, to_date))
	)

	if user:
		deals_query = deals_query.where(Deal.deal_owner == user)

	deals_query = deals_query.groupby(Date(Deal.creation))

	# Combine with UNION ALL and aggregate by date
	union_query = leads_query.union_all(deals_query)

	# Wrap in outer query to aggregate by date
	daily = (
		frappe.qb.from_(union_query)
		.select(
			DateFormat(union_query.date, "%Y-%m-%d").as_("date"),
			Sum(union_query.leads).as_("leads"),
			Sum(union_query.deals).as_("deals"),
			Sum(union_query.won_deals).as_("won_deals"),
		)
		.groupby(union_query.date)
		.orderby(union_query.date)
	)

	result = daily.run(as_dict=True)

	sales_trend = [
		{
			"date": frappe.utils.get_datetime(row.date).strftime("%Y-%m-%d"),
			"leads": row.leads or 0,
			"deals": row.deals or 0,
			"won_deals": row.won_deals or 0,
		}
		for row in result
	]

	return {
		"data": sales_trend,
		"title": _("Sales trend"),
		"subtitle": _("Daily performance of leads, deals, and wins"),
		"xAxis": {
			"title": _("Date"),
			"key": "date",
			"type": "time",
			"timeGrain": "day",
		},
		"yAxis": {
			"title": _("Count"),
		},
		"series": [
			{"name": "leads", "type": "line", "showDataPoints": True},
			{"name": "deals", "type": "line", "showDataPoints": True},
			{"name": "won_deals", "type": "line", "showDataPoints": True},
		],
	}


def get_forecasted_revenue(from_date: str | None = None, to_date: str | None = None, user: str | None = None):
	"""
	Get forecasted revenue for the dashboard.
	[
		{ date: new Date('2024-05-01'), forecasted: 1200000, actual: 980000 },
		{ date: new Date('2024-06-01'), forecasted: 1350000, actual: 1120000 },
		{ date: new Date('2024-07-01'), forecasted: 1600000, actual: "" },
		{ date: new Date('2024-08-01'), forecasted: 1500000, actual: "" },
		...
	]
	"""
	# Using Frappe Query Builder with CASE expressions
	CRMDeal = DocType("CRM Deal")
	CRMDealStatus = DocType("CRM Deal Status")

	# Calculate the date 12 months ago
	twelve_months_ago = frappe.utils.add_months(frappe.utils.nowdate(), -12)

	forecasted_value = (
		Case()
		.when(CRMDealStatus.type == "Lost", CRMDeal.expected_deal_value * IfNull(CRMDeal.exchange_rate, 1))
		.else_(
			CRMDeal.expected_deal_value
			* IfNull(CRMDeal.probability, 0)
			/ 100
			* IfNull(CRMDeal.exchange_rate, 1)
		)
	)

	actual_value = (
		Case()
		.when(CRMDealStatus.type == "Won", CRMDeal.deal_value * IfNull(CRMDeal.exchange_rate, 1))
		.else_(0)
	)

	query = (
		frappe.qb.from_(CRMDeal)
		.join(CRMDealStatus)
		.on(CRMDeal.status == CRMDealStatus.name)
		.select(
			DateFormat(CRMDeal.expected_closure_date, "%Y-%m").as_("month"),
			Sum(forecasted_value).as_("forecasted"),
			Sum(actual_value).as_("actual"),
		)
		.where(CRMDeal.expected_closure_date >= twelve_months_ago)
		.groupby(DateFormat(CRMDeal.expected_closure_date, "%Y-%m"))
		.orderby(DateFormat(CRMDeal.expected_closure_date, "%Y-%m"))
	)

	if user:
		query = query.where(CRMDeal.deal_owner == user)

	result = query.run(as_dict=True)

	for row in result:
		row["month"] = frappe.utils.get_datetime(row["month"]).strftime("%Y-%m-01")
		row["forecasted"] = row["forecasted"] or ""
		row["actual"] = row["actual"] or ""

	return {
		"data": result or [],
		"title": _("Forecasted revenue"),
		"subtitle": _("Projected vs actual revenue based on deal probability"),
		"xAxis": {
			"title": _("Month"),
			"key": "month",
			"type": "time",
			"timeGrain": "month",
		},
		"yAxis": {
			"title": _("Revenue") + f" ({get_base_currency_symbol()})",
		},
		"series": [
			{"name": "forecasted", "type": "line", "showDataPoints": True},
			{"name": "actual", "type": "line", "showDataPoints": True},
		],
	}


def get_funnel_conversion(from_date: str | None = None, to_date: str | None = None, user: str | None = None):
	"""
	Get funnel conversion data for the dashboard.
	[
		{ stage: 'Leads', count: 120 },
		{ stage: 'Qualification', count: 100 },
		{ stage: 'Negotiation', count: 80 },
		{ stage: 'Ready to Close', count: 60 },
		{ stage: 'Won', count: 30 },
		...
	]
	"""
	lead_conds = ""
	deal_conds = ""

	if not from_date or not to_date:
		from_date = frappe.utils.get_first_day(from_date or frappe.utils.nowdate())
		to_date = frappe.utils.get_last_day(to_date or frappe.utils.nowdate())

	lead_filters = {"from": from_date, "to": to_date}
	deal_filters = {"from": from_date, "to": to_date}

	if user:
		lead_conds += " AND lead_owner = %(user)s"
		deal_conds += " AND deal_owner = %(user)s"
		lead_filters["user"] = user
		deal_filters["user"] = user

	result = []

	# Get total leads using Query Builder
	CRMLead = DocType("CRM Lead")

	query = (
		frappe.qb.from_(CRMLead)
		.select(Count("*").as_("count"))
		.where(Date(CRMLead.creation).between(from_date, to_date))
	)

	if user:
		query = query.where(CRMLead.lead_owner == user)

	total_leads = query.run(as_dict=True)
	total_leads_count = total_leads[0].count if total_leads else 0

	result.append({"stage": "Leads", "count": total_leads_count})

	result += get_deal_status_change_counts(from_date, to_date, deal_conds, deal_filters)

	return {
		"data": result or [],
		"title": _("Funnel conversion"),
		"subtitle": _("Lead to deal conversion pipeline"),
		"xAxis": {
			"title": _("Stage"),
			"key": "stage",
			"type": "category",
		},
		"yAxis": {
			"title": _("Count"),
		},
		"swapXY": True,
		"series": [
			{
				"name": "count",
				"type": "bar",
				"echartOptions": {
					"colorBy": "data",
				},
			},
		],
	}


def get_deals_by_stage_axis(
	from_date: str | None = None, to_date: str | None = None, user: str | None = None
):
	"""
	Get deal data by stage for the dashboard.
	[
		{ stage: 'Prospecting', count: 120 },
		{ stage: 'Negotiation', count: 45 },
		...
	]
	"""
	if not from_date or not to_date:
		from_date = frappe.utils.get_first_day(from_date or frappe.utils.nowdate())
		to_date = frappe.utils.get_last_day(to_date or frappe.utils.nowdate())

	# Using Frappe Query Builder with NOT IN clause
	CRMDeal = DocType("CRM Deal")
	CRMDealStatus = DocType("CRM Deal Status")

	query = (
		frappe.qb.from_(CRMDeal)
		.join(CRMDealStatus)
		.on(CRMDeal.status == CRMDealStatus.name)
		.select(CRMDeal.status.as_("stage"), Count("*").as_("count"), CRMDealStatus.type.as_("status_type"))
		.where((Date(CRMDeal.creation).between(from_date, to_date)) & (CRMDealStatus.type.notin(["Lost"])))
		.groupby(CRMDeal.status)
		.orderby(Count("*"), order=frappe.qb.desc)
	)

	if user:
		query = query.where(CRMDeal.deal_owner == user)

	result = query.run(as_dict=True)

	return {
		"data": result or [],
		"title": _("Deals by ongoing & won stage"),
		"xAxis": {
			"title": _("Stage"),
			"key": "stage",
			"type": "category",
		},
		"yAxis": {"title": _("Count")},
		"series": [
			{"name": "count", "type": "bar"},
		],
	}


def get_deals_by_stage_donut(
	from_date: str | None = None, to_date: str | None = None, user: str | None = None
):
	"""
	Get deal data by stage for the dashboard.
	[
		{ stage: 'Prospecting', count: 120 },
		{ stage: 'Negotiation', count: 45 },
		...
	]
	"""
	if not from_date or not to_date:
		from_date = frappe.utils.get_first_day(from_date or frappe.utils.nowdate())
		to_date = frappe.utils.get_last_day(to_date or frappe.utils.nowdate())

	# Using Frappe Query Builder with JOIN
	CRMDeal = DocType("CRM Deal")
	CRMDealStatus = DocType("CRM Deal Status")

	query = (
		frappe.qb.from_(CRMDeal)
		.join(CRMDealStatus)
		.on(CRMDeal.status == CRMDealStatus.name)
		.select(CRMDeal.status.as_("stage"), Count("*").as_("count"), CRMDealStatus.type.as_("status_type"))
		.where(Date(CRMDeal.creation).between(from_date, to_date))
		.groupby(CRMDeal.status)
		.orderby(Count("*"), order=frappe.qb.desc)
	)

	if user:
		query = query.where(CRMDeal.deal_owner == user)

	result = query.run(as_dict=True)

	return {
		"data": result or [],
		"title": _("Deals by stage"),
		"subtitle": _("Current pipeline distribution"),
		"categoryColumn": "stage",
		"valueColumn": "count",
	}


def get_lost_deal_reasons(from_date: str | None = None, to_date: str | None = None, user: str | None = None):
	"""
	Lost deal reasons across all deals.

	Ignores from_date/to_date. This is a *pattern discovery* metric — you want to
	know which reasons keep coming up, and a period-scoped view suppresses the ones
	that hurt you consistently but only fire once or twice a quarter. All-time is
	the meaningful scope. User filter still applies.
	"""
	CRMDeal = DocType("CRM Deal")
	CRMDealStatus = DocType("CRM Deal Status")

	query = (
		frappe.qb.from_(CRMDeal)
		.join(CRMDealStatus)
		.on(CRMDeal.status == CRMDealStatus.name)
		.select(CRMDeal.lost_reason.as_("reason"), Count("*").as_("count"))
		.where(CRMDealStatus.type == "Lost")
		.groupby(CRMDeal.lost_reason)
		.having((CRMDeal.lost_reason.isnotnull()) & (CRMDeal.lost_reason != ""))
		.orderby(Count("*"), order=frappe.qb.desc)
	)

	if user:
		query = query.where(CRMDeal.deal_owner == user)

	result = query.run(as_dict=True)

	return {
		"data": result or [],
		"title": _("Lost deal reasons"),
		"subtitle": _("All-time — common reasons for losing deals"),
		"xAxis": {
			"title": _("Reason"),
			"key": "reason",
			"type": "category",
		},
		"yAxis": {
			"title": _("Count"),
		},
		"series": [
			{"name": "count", "type": "bar"},
		],
	}


def get_leads_by_source(from_date: str | None = None, to_date: str | None = None, user: str | None = None):
	"""
	Get lead data by source for the dashboard.
	[
		{ source: 'Website', count: 120 },
		{ source: 'Referral', count: 45 },
		...
	]
	"""
	if not from_date or not to_date:
		from_date = frappe.utils.get_first_day(from_date or frappe.utils.nowdate())
		to_date = frappe.utils.get_last_day(to_date or frappe.utils.nowdate())

	# Using Frappe Query Builder (safer, more maintainable)
	CRMLead = DocType("CRM Lead")

	query = (
		frappe.qb.from_(CRMLead)
		.select(IfNull(CRMLead.source, "Empty").as_("source"), Count("*").as_("count"))
		.where(Date(CRMLead.creation).between(from_date, to_date))
		.groupby(CRMLead.source)
		.orderby(Count("*"), order=frappe.qb.desc)
	)

	if user:
		query = query.where(CRMLead.lead_owner == user)

	result = query.run(as_dict=True)

	return {
		"data": result or [],
		"title": _("Leads by source"),
		"subtitle": _("Lead generation channel analysis"),
		"categoryColumn": "source",
		"valueColumn": "count",
	}


def get_deals_by_source(from_date: str | None = None, to_date: str | None = None, user: str | None = None):
	"""
	Get deal data by source for the dashboard.
	[
		{ source: 'Website', count: 120 },
		{ source: 'Referral', count: 45 },
		...
	]
	"""
	if not from_date or not to_date:
		from_date = frappe.utils.get_first_day(from_date or frappe.utils.nowdate())
		to_date = frappe.utils.get_last_day(to_date or frappe.utils.nowdate())

	# Using Frappe Query Builder
	CRMDeal = DocType("CRM Deal")

	query = (
		frappe.qb.from_(CRMDeal)
		.select(IfNull(CRMDeal.source, "Empty").as_("source"), Count("*").as_("count"))
		.where(Date(CRMDeal.creation).between(from_date, to_date))
		.groupby(CRMDeal.source)
		.orderby(Count("*"), order=frappe.qb.desc)
	)

	if user:
		query = query.where(CRMDeal.deal_owner == user)

	result = query.run(as_dict=True)

	return {
		"data": result or [],
		"title": _("Deals by source"),
		"subtitle": _("Deal generation channel analysis"),
		"categoryColumn": "source",
		"valueColumn": "count",
	}


def get_deals_by_territory(from_date: str | None = None, to_date: str | None = None, user: str | None = None):
	"""
	Get deal data by territory for the dashboard.
	[
		{ territory: 'North America', deals: 45, value: 2300000 },
		{ territory: 'Europe', deals: 30, value: 1500000 },
		...
	]
	"""
	if not from_date or not to_date:
		from_date = frappe.utils.get_first_day(from_date or frappe.utils.nowdate())
		to_date = frappe.utils.get_last_day(to_date or frappe.utils.nowdate())

	# Using Frappe Query Builder with complex aggregations
	CRMDeal = DocType("CRM Deal")

	query = (
		frappe.qb.from_(CRMDeal)
		.select(
			IfNull(CRMDeal.territory, "Empty").as_("territory"),
			Count("*").as_("deals"),
			Sum(Coalesce(CRMDeal.deal_value, 0) * IfNull(CRMDeal.exchange_rate, 1)).as_("value"),
		)
		.where(Date(CRMDeal.creation).between(from_date, to_date))
		.groupby(CRMDeal.territory)
		.orderby(Count("*"), order=frappe.qb.desc)
		.orderby(
			Sum(Coalesce(CRMDeal.deal_value, 0) * IfNull(CRMDeal.exchange_rate, 1)), order=frappe.qb.desc
		)
	)

	if user:
		query = query.where(CRMDeal.deal_owner == user)

	result = query.run(as_dict=True)

	return {
		"data": result or [],
		"title": _("Deals by territory"),
		"subtitle": _("Geographic distribution of deals and revenue"),
		"xAxis": {
			"title": _("Territory"),
			"key": "territory",
			"type": "category",
		},
		"yAxis": {
			"title": _("Number of deals"),
		},
		"y2Axis": {
			"title": _("Deal value") + f" ({get_base_currency_symbol()})",
		},
		"series": [
			{"name": "deals", "type": "bar"},
			{"name": "value", "type": "line", "showDataPoints": True, "axis": "y2"},
		],
	}


def get_deals_by_salesperson(
	from_date: str | None = None, to_date: str | None = None, user: str | None = None
):
	"""
	Get deal data by salesperson for the dashboard.
	[
		{ salesperson: 'John Smith', deals: 45, value: 2300000 },
		{ salesperson: 'Jane Doe', deals: 30, value: 1500000 },
		...
	]
	"""
	if not from_date or not to_date:
		from_date = frappe.utils.get_first_day(from_date or frappe.utils.nowdate())
		to_date = frappe.utils.get_last_day(to_date or frappe.utils.nowdate())

	# Using Frappe Query Builder with LEFT JOIN
	CRMDeal = DocType("CRM Deal")
	User = DocType("User")

	# Filter by `modified` (any activity in the window) rather than `creation`. A "deals by
	# salesperson" chart should represent workload in the period, not just newly-created deals —
	# otherwise salespeople whose deals were opened before the window disappear entirely, even
	# when they moved those deals through stages during the window.
	query = (
		frappe.qb.from_(CRMDeal)
		.left_join(User)
		.on(User.name == CRMDeal.deal_owner)
		.select(
			IfNull(User.full_name, CRMDeal.deal_owner).as_("salesperson"),
			Count("*").as_("deals"),
			Sum(Coalesce(CRMDeal.deal_value, 0) * IfNull(CRMDeal.exchange_rate, 1)).as_("value"),
		)
		.where(Date(CRMDeal.modified).between(from_date, to_date))
		.where(CRMDeal.deal_owner.isnotnull() & (CRMDeal.deal_owner != ""))
		.groupby(CRMDeal.deal_owner)
		.orderby(Count("*"), order=frappe.qb.desc)
		.orderby(
			Sum(Coalesce(CRMDeal.deal_value, 0) * IfNull(CRMDeal.exchange_rate, 1)), order=frappe.qb.desc
		)
	)

	if user:
		query = query.where(CRMDeal.deal_owner == user)

	result = query.run(as_dict=True)

	return {
		"data": result or [],
		"title": _("Deals by salesperson"),
		"subtitle": _("Number of deals and total value per salesperson"),
		"xAxis": {
			"title": _("Salesperson"),
			"key": "salesperson",
			"type": "category",
		},
		"yAxis": {
			"title": _("Number of deals"),
		},
		"y2Axis": {
			"title": _("Deal value") + f" ({get_base_currency_symbol()})",
		},
		"series": [
			{"name": "deals", "type": "bar"},
			{"name": "value", "type": "line", "showDataPoints": True, "axis": "y2"},
		],
	}


def get_win_rate_trend(
	from_date: str | None = None, to_date: str | None = None, user: str | None = None
):
	"""
	Win rate over time: won / (won + lost) per month, over the last 12 months.

	Ignores from_date/to_date on purpose — the window filters throughout the rest of
	the dashboard drive current-period comparisons, but win rate is only meaningful
	as a trend, so we hold a fixed 12-month rolling window. The other filters (user)
	still apply.
	"""
	Deal = DocType("CRM Deal")
	Status = DocType("CRM Deal Status")

	twelve_months_ago = frappe.utils.add_months(frappe.utils.nowdate(), -12)

	won_expr = Sum(Case().when(Status.type == "Won", 1).else_(0))
	lost_expr = Sum(Case().when(Status.type == "Lost", 1).else_(0))

	query = (
		frappe.qb.from_(Deal)
		.join(Status)
		.on(Deal.status == Status.name)
		.select(
			DateFormat(Deal.closed_date, "%Y-%m").as_("month"),
			won_expr.as_("won"),
			lost_expr.as_("lost"),
		)
		.where(Deal.closed_date.isnotnull())
		.where(Deal.closed_date >= twelve_months_ago)
		.where(Status.type.isin(["Won", "Lost"]))
		.groupby(DateFormat(Deal.closed_date, "%Y-%m"))
		.orderby(DateFormat(Deal.closed_date, "%Y-%m"))
	)

	if user:
		query = query.where(Deal.deal_owner == user)

	rows = query.run(as_dict=True)

	data = []
	for row in rows:
		won = int(row["won"] or 0)
		lost = int(row["lost"] or 0)
		closed = won + lost
		# Skip months where nothing closed — a 0/0 point on the chart reads as "0%
		# win rate" which is misleading; better to omit the month.
		if closed == 0:
			continue
		data.append(
			{
				"month": frappe.utils.get_datetime(row["month"]).strftime("%Y-%m-01"),
				"win_rate": round(100.0 * won / closed, 1),
				"won": won,
				"lost": lost,
			}
		)

	return {
		"data": data,
		"title": _("Win rate over time"),
		"subtitle": _("Won / (Won + Lost) closed deals per month, last 12 months"),
		"xAxis": {
			"title": _("Month"),
			"key": "month",
			"type": "time",
			"timeGrain": "month",
		},
		"yAxis": {
			"title": _("Win rate (%)"),
		},
		"series": [
			{"name": "win_rate", "type": "line", "showDataPoints": True},
		],
	}


def get_data_quality(
	from_date: str | None = None, to_date: str | None = None, user: str | None = None
):
	"""
	Data-quality snapshot: for each dashboard-relevant deal field, the percentage of
	deals that have it populated.

	Ignores from_date/to_date on purpose. Data quality is a *discipline* metric — it
	measures your team's ongoing fill-in habits, not a period snapshot. Applying the
	global window makes the percentages arbitrary (small window -> small denominator,
	one missing field reads as 0% or 100%, no signal). All-time is the meaningful
	scope. The user filter still applies so a manager can scope to one salesperson.

	Purpose is to make the reason behind empty tiles visible. E.g. Avg. Won Deal
	Value at 0 could be zero because there are no won deals, or because won deals
	were closed without a `deal_value` — this chart tells you which.
	"""
	Deal = DocType("CRM Deal")
	Status = DocType("CRM Deal Status")

	user_cond = (Deal.deal_owner == user) if user else None

	def _run(where_extra=None):
		"""Return a query over all deals matching the user filter (no date scope)."""
		q = frappe.qb.from_(Deal).join(Status).on(Deal.status == Status.name)
		if user_cond is not None:
			q = q.where(user_cond)
		if where_extra is not None:
			q = q.where(where_extra)
		return q

	def _totals():
		q = _run().select(Count("*").as_("total"))
		return int(q.run(as_dict=True)[0]["total"])

	def _populated(cond, extra=None):
		q = _run(extra).select(Sum(Case().when(cond, 1).else_(0)).as_("populated"))
		return int(q.run(as_dict=True)[0]["populated"] or 0)

	total_deals = _totals()

	# Won deals need a separate denominator (deal_value is only meaningful post-close).
	won_query = _run(Status.type == "Won").select(Count("*").as_("n"))
	won_total = int(won_query.run(as_dict=True)[0]["n"])

	def _pct(numer, denom):
		# Return None (not 0) when there's nothing to divide — the frontend renders
		# that as "N/A" instead of a misleading 0% bar. A 0/0 field-fill rate is
		# undefined; showing "0%" would read as "team is failing at this field".
		return round(100.0 * numer / denom, 1) if denom else None

	fields = []

	# Deal Value on Won deals — the field that drives Avg. Won Deal Value.
	won_with_value = _populated(Deal.deal_value > 0, Status.type == "Won")
	fields.append(
		{
			"field": _("Deal Value (Won only)"),
			"populated_pct": _pct(won_with_value, won_total),
			"populated": won_with_value,
			"total": won_total,
		}
	)

	# Expected Deal Value on non-lost deals (drives forecasted revenue + pipeline value).
	non_lost = _run(Status.type != "Lost").select(Count("*").as_("n"))
	non_lost_total = int(non_lost.run(as_dict=True)[0]["n"])
	non_lost_with_expected = _populated(Deal.expected_deal_value > 0, Status.type != "Lost")
	fields.append(
		{
			"field": _("Expected Deal Value (non-Lost)"),
			"populated_pct": _pct(non_lost_with_expected, non_lost_total),
			"populated": non_lost_with_expected,
			"total": non_lost_total,
		}
	)

	# Expected Closure Date on non-lost deals (drives forecasted revenue).
	non_lost_with_expected_close = _populated(
		Deal.expected_closure_date.isnotnull(), Status.type != "Lost"
	)
	fields.append(
		{
			"field": _("Expected Closure Date (non-Lost)"),
			"populated_pct": _pct(non_lost_with_expected_close, non_lost_total),
			"populated": non_lost_with_expected_close,
			"total": non_lost_total,
		}
	)

	# Linked Lead on all deals in window (drives Avg. Time to Close a Lead).
	deals_with_lead = _populated(Deal.lead.isnotnull() & (Deal.lead != ""))
	fields.append(
		{
			"field": _("Linked Lead"),
			"populated_pct": _pct(deals_with_lead, total_deals),
			"populated": deals_with_lead,
			"total": total_deals,
		}
	)

	# Deal Owner on all deals (drives Deals by Salesperson).
	deals_with_owner = _populated(Deal.deal_owner.isnotnull() & (Deal.deal_owner != ""))
	fields.append(
		{
			"field": _("Deal Owner"),
			"populated_pct": _pct(deals_with_owner, total_deals),
			"populated": deals_with_owner,
			"total": total_deals,
		}
	)

	return {
		"data": fields,
		"title": _("Data quality"),
		"subtitle": _("% of deals with each dashboard-relevant field set (current window)"),
		"xAxis": {
			"title": _("Field"),
			"key": "field",
			"type": "category",
		},
		"yAxis": {
			"title": _("% populated"),
		},
		"series": [
			{"name": "populated_pct", "type": "bar"},
		],
	}


def get_base_currency_symbol():
	"""
	Get the base currency symbol from the system settings.
	"""
	base_currency = frappe.db.get_single_value("FCRM Settings", "currency") or "USD"
	return frappe.db.get_value("Currency", base_currency, "symbol") or ""


def get_deal_status_change_counts(
	from_date: str | None = None,
	to_date: str | None = None,
	deal_conds: str = "",
	filters: dict | None = None,
):
	"""
	Get count of each status change (to) for each deal, excluding deals with current status type 'Lost'.
	Order results by status position.
	Returns:
	[
	  {"status": "Qualification", "count": 120},
	  {"status": "Negotiation", "count": 85},
	  ...
	]
	"""
	# Using Frappe Query Builder with multiple JOINs and table aliases
	CRMStatusChangeLog = DocType("CRM Status Change Log")
	CRMDeal = DocType("CRM Deal")
	CurrentStatus = DocType("CRM Deal Status").as_("s")
	TargetStatus = DocType("CRM Deal Status").as_("st")

	query = (
		frappe.qb.from_(CRMStatusChangeLog)
		.join(CRMDeal)
		.on(CRMStatusChangeLog.parent == CRMDeal.name)
		.join(CurrentStatus)
		.on(CRMDeal.status == CurrentStatus.name)
		.join(TargetStatus)
		.on(CRMStatusChangeLog.to == TargetStatus.name)
		.select(CRMStatusChangeLog.to.as_("stage"), Count("*").as_("count"))
		.where(
			(CRMStatusChangeLog.to.isnotnull())
			& (CRMStatusChangeLog.to != "")
			& (CurrentStatus.type != "Lost")
			& (Date(CRMDeal.creation).between(from_date, to_date))
		)
		.groupby(CRMStatusChangeLog.to, TargetStatus.position)
		.orderby(TargetStatus.position)
	)

	# Handle optional user filter if deal_conds contains user condition
	if filters and filters.get("user"):
		query = query.where(CRMDeal.deal_owner == filters["user"])

	result = query.run(as_dict=True)
	return result or []
