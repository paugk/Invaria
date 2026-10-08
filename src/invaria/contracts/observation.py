"""Observation: an immutable statement by one source, with provenance.

Corrections are new revisions that point to the revision they replace (``supersedes``).
Retractions are revisions without payload. Nothing is edited in place.
"""

from __future__ import annotations

import base64
import re
from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, model_validator

from invaria.contracts.base import (
    Contract,
    Identifier,
    SchemaVersion,
    Sha256Hex,
    UtcDatetime,
    VersionRef,
)
from invaria.contracts.quantity import Atoms, Quantity
from invaria.contracts.stellar import (
    StellarAccountId,
    StellarNetwork,
    U64Text,
    decode_account_id,
    decode_strkey,
    encode_muxed_account,
    parse_u64,
)

FactType = Literal[
    "order_accepted",
    "cash_settled",
    "units_registered",
    "token_movement",
    # Redemption: request, cancellation and position attested by the TA; the
    # approved price by the pricing source.
    "redemption_accepted",
    "redemption_cancelled",
    "position_held",
    "price_approved",
    "position_changed",
    "redemption_reactivated",
    # On-chain effects kept apart from their business interpretation. They are
    # not admitted as evidence of fulfilment by the current profiles: evidence, not
    # deliveries, burns or payments.
    "chain_effect",
]


class SourceRecord(Contract):
    """Idempotency identity of a source record: same key + revision = same statement."""

    source_id: Identifier
    record_key: Identifier
    revision: Annotated[int, Field(ge=1)]


class Provenance(Contract):
    raw_sha256: Sha256Hex
    raw_locator: Annotated[str, Field(min_length=1, max_length=512)]
    parser_ref: VersionRef
    mapping_ref: VersionRef


class OrderPayload(Contract):
    payload_type: Literal["order_accepted"]
    account_ref: Identifier
    units: Quantity
    cash_amount: Quantity
    price_per_unit: Quantity


class CashPayload(Contract):
    payload_type: Literal["cash_settled"]
    account_ref: Identifier
    payment_ref: Identifier
    amount: Quantity


class UnitsPayload(Contract):
    payload_type: Literal["units_registered"]
    account_ref: Identifier
    units: Quantity


class ChainCoordinates(Contract):
    network: StellarNetwork
    ledger: Annotated[int, Field(ge=1)]
    tx_hash: Sha256Hex
    operation_index: Annotated[int, Field(ge=0)]
    tx_successful: bool


def _absent_if_none(value: object) -> bool:
    return value is None


MemoType = Literal["none", "text", "id", "hash", "return"]
_BASE64 = re.compile(r"^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$")
_HEX32 = re.compile(r"^[0-9a-f]{64}$")


class TransactionMemo(Contract):
    """The memo of the whole transaction, exactly as the ledger holds it.

    Its scope is the transaction (``ChainCoordinates.tx_hash``), never one operation, and
    it is context: it never links an observation to an operation (only an explicit
    ExecutionLink does) and it never identifies a muxed sub-account. A memo id and a muxed id
    that are numerically equal are different things.

    ``value`` by type: ``none`` none; ``text`` the exact bytes (0 to 28, not necessarily
    UTF-8) in padded base64; ``id`` a canonical decimal u64; ``hash`` and ``return`` the 32
    bytes in lowercase hex.
    """

    memo_type: MemoType
    value: str | None

    @model_validator(mode="after")
    def _exact(self) -> Self:
        kind, value = self.memo_type, self.value
        if kind == "none":
            if value is not None:
                raise ValueError("a transaction without memo has no memo value")
            return self
        if value is None:
            raise ValueError(f"a {kind} memo has a value")
        if kind == "text":
            if not _BASE64.match(value) or len(base64.b64decode(value)) > 28:
                raise ValueError("a text memo is at most 28 bytes, in padded base64")
            if base64.b64encode(base64.b64decode(value)).decode("ascii") != value:
                raise ValueError("a text memo is in canonical base64 (zero padding bits)")
        elif kind == "id":
            parse_u64(value)
        elif not _HEX32.match(value):
            raise ValueError(f"a {kind} memo is 32 bytes in lowercase hex")
        return self


class TokenMovementPayload(Contract):
    """A movement of the target asset between two base accounts.

    ``from_muxed_id``/``to_muxed_id``: the sub-account id of a muxed
    (M) sender or receiver, as the Classic operation names it; None when that side is not
    muxed (an id of 0 is a muxed id). The balance that moves is the base account's; who holds
    the sub-account is not known from the id, and an IdentityLink to the base account does
    not approve it. ``memo`` is the transaction's memo when the representation read
    it (the Classic payment, mapping 1.1.0 on); None means it was not read, never that the
    transaction has none (that is ``memo_type: none``). All three are absent from the
    serialized form when None, so earlier artifacts stay identical.
    """

    payload_type: Literal["token_movement"]
    from_address: StellarAccountId
    to_address: StellarAccountId
    units: Quantity
    chain: ChainCoordinates
    from_muxed_id: U64Text | None = Field(default=None, exclude_if=_absent_if_none)
    to_muxed_id: U64Text | None = Field(default=None, exclude_if=_absent_if_none)
    memo: TransactionMemo | None = Field(default=None, exclude_if=_absent_if_none)


def movement_sender(payload: TokenMovementPayload) -> str:
    """The sender as a ledger address: the M strkey of a muxed sender, else its G account."""
    if payload.from_muxed_id is None:
        return payload.from_address
    return encode_muxed_account(payload.from_address, parse_u64(payload.from_muxed_id))


def movement_receiver(payload: TokenMovementPayload) -> str:
    """The receiver as a ledger address: the M strkey of a muxed receiver, else its G account."""
    if payload.to_muxed_id is None:
        return payload.to_address
    return encode_muxed_account(payload.to_address, parse_u64(payload.to_muxed_id))


def is_muxed(payload: TokenMovementPayload) -> bool:
    return payload.from_muxed_id is not None or payload.to_muxed_id is not None


def movement_addresses(payload: TokenMovementPayload) -> set[str]:
    """Every ledger address a movement names: both base accounts and any M sub-account."""
    return {
        payload.from_address,
        payload.to_address,
        movement_sender(payload),
        movement_receiver(payload),
    }


class RedemptionRequestPayload(Contract):
    """A redemption request accepted by the TA: a fixed quantity, settled in full.

    ``accepted_at`` and ``payment_due_at`` are both explicit; the profile fixes how the
    second follows from the first, and the engine checks it rather than trusting it.
    """

    payload_type: Literal["redemption_accepted"]
    account_ref: Identifier
    units: Quantity
    price_ref: Identifier
    price_per_unit: Quantity
    accepted_at: UtcDatetime
    payment_due_at: UtcDatetime
    journal_id: Identifier  # source TA journal; sequences compare only within one journal
    sequence: Annotated[int, Field(ge=1)]  # source-assigned order of the acceptance entry
    settlement_ref: Identifier | None  # payment reference the TA instructed, if any


class RedemptionCancellationPayload(Contract):
    """A business cancellation of a request, distinct from retracting evidence."""

    payload_type: Literal["redemption_cancelled"]
    account_ref: Identifier
    cancelled_at: UtcDatetime
    authorized_by: Identifier


class PositionPayload(Contract):
    """Holding and pending reservations of the account at a TA journal point.

    ``sequence`` orders the snapshot against the TA journal: it includes every entry up to
    and including that sequence. Equal timestamps never order entries by themselves.
    """

    payload_type: Literal["position_held"]
    account_ref: Identifier
    units: Quantity
    reserved: Quantity
    as_of: UtcDatetime
    journal_id: Identifier
    sequence: Annotated[int, Field(ge=1)]


class PositionChangePayload(Contract):
    """A TA journal entry that moves the holding or the reservations of an account."""

    payload_type: Literal["position_changed"]
    account_ref: Identifier
    change_ref: Identifier
    holding_delta: Quantity
    reserved_delta: Quantity
    effective_at: UtcDatetime
    journal_id: Identifier
    sequence: Annotated[int, Field(ge=1)]
    request_ref: Identifier | None


class RedemptionReactivationPayload(Contract):
    """A business reactivation of a cancelled request (unsupported in this profile)."""

    payload_type: Literal["redemption_reactivated"]
    account_ref: Identifier
    reactivated_at: UtcDatetime
    authorized_by: Identifier


class PricePayload(Contract):
    """An approved, versioned price per unit (``price_ref`` names the version)."""

    payload_type: Literal["price_approved"]
    price_ref: Identifier
    price_per_unit: Quantity


ChainAsset = Annotated[
    str, StringConstraints(pattern=r"^(native|[A-Za-z0-9]{1,12}:G[A-Z2-7]{55})$")
]


PartyKind = Literal["account", "contract", "claimable_balance", "liquidity_pool", "unresolved"]


class ChainParty(Contract):
    """A typed technical entity on the ledger.

    None of them, an account included, is by itself an economic holder, a beneficiary or a
    person: a contract's owner, a claimable balance's eventual recipient and a pool's
    liquidity providers are never inferred from the identifier. ``id`` is the native
    identifier in its lossless SEP-23 strkey form (G account, C contract, B claimable
    balance, L liquidity pool), so a contract, balance or pool never reads as an account.
    An ``unresolved`` party is one the evidence does not identify: it has no id and says
    why. A party's identity is its kind and id on the network of the effect that names it
    (``party_identity``).
    """

    kind: PartyKind
    # Absent from the serialized form when None, so account and contract parties of
    # earlier artifacts stay identical.
    id: Annotated[str, StringConstraints(min_length=1, max_length=128)] | None = Field(
        default=None, exclude_if=_absent_if_none
    )
    reason: Annotated[str, StringConstraints(min_length=1, max_length=200)] | None = Field(
        default=None, exclude_if=_absent_if_none
    )

    @model_validator(mode="after")
    def _typed_id(self) -> Self:
        if self.kind == "unresolved":
            if self.id is not None or self.reason is None:
                raise ValueError("an unresolved party has no id and says why")
            return self
        if self.id is None or self.reason is not None:
            raise ValueError("a resolved party has an id and no unresolved reason")
        if self.kind == "account":
            decode_account_id(self.id)
        else:
            decode_strkey(self.kind, self.id)
        return self


def party_identity(network: StellarNetwork, party: ChainParty) -> str:
    """Identity of a typed party: network, kind and native id. Equal ids on two networks,
    or two kinds, are never the same party. Every unresolved party gives the same string
    (``<network>/unresolved/?``): it identifies nobody, so it must never be used to match
    or merge records."""
    return f"{network}/{party.kind}/{party.id if party.id is not None else '?'}"


class TransactionOutcome(Contract):
    """The whole transaction's technical result: fee (XLM stroops) and Horizon's
    ``result_xdr`` when it reports one. Repeated on each effect of the operation for
    context; it is never apportioned nor summed per effect."""

    fee_account: StellarAccountId
    fee_charged: Quantity
    result_xdr: (
        Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9+/]+={0,2}$", max_length=4096)] | None
    )
    # The transaction's memo (mapping 1.1.0 on); None when not read.
    memo: TransactionMemo | None = Field(default=None, exclude_if=_absent_if_none)

    @model_validator(mode="after")
    def _xlm_stroops(self) -> Self:
        if (self.fee_charged.scale, self.fee_charged.unit) != (7, "XLM"):
            raise ValueError("fee_charged is XLM at stroop scale (7)")
        if self.fee_charged.atoms.startswith("-"):
            raise ValueError("fee_charged cannot be negative")
        return self


class PathPaymentContext(Contract):
    """The Classic operation as Horizon reports it. Amounts are executed amounts (None
    when the operation failed); declared limits (sendMax, destMin) are never used."""

    operation_type: Literal["path_payment_strict_send", "path_payment_strict_receive"]
    source_account: StellarAccountId
    destination_account: StellarAccountId
    source_asset: ChainAsset
    destination_asset: ChainAsset
    source_amount: Atoms | None  # stroop-scale (7) atoms of source_asset
    destination_amount: Atoms | None  # stroop-scale (7) atoms of destination_asset
    path: list[ChainAsset]


DexOperation = Literal[
    "manage_sell_offer",
    "manage_buy_offer",
    "create_passive_sell_offer",
    "path_payment_strict_send",
    "path_payment_strict_receive",
]


class ExchangeContext(Contract):
    """DEX origin of a fill, as Horizon reports the operation and, for a Classic fill, the
    trade. ``operation_source`` is a technical party: a fill is an effect on the offer
    owner's account, never a payment by or to whoever submitted the operation."""

    operation_type: DexOperation
    operation_source: StellarAccountId
    offer_id: Annotated[str, StringConstraints(pattern=r"^[0-9]{1,20}$")] | None
    sold_asset: ChainAsset | None
    sold_amount: Atoms | None  # stroop-scale (7) atoms of sold_asset
    bought_asset: ChainAsset | None
    bought_amount: Atoms | None  # stroop-scale (7) atoms of bought_asset

    @model_validator(mode="after")
    def _trade(self) -> Self:
        trade = (
            self.offer_id,
            self.sold_asset,
            self.sold_amount,
            self.bought_asset,
            self.bought_amount,
        )
        if any(v is None for v in trade) and any(v is not None for v in trade):
            raise ValueError("trade fields are all present (Classic) or all absent (SAC)")
        return self


class UnresolvedOrigin(Contract):
    """Why a SAC movement could not be told apart from a DEX fill or another effect."""

    reason: Literal[
        "operation_not_found",
        "unsupported_operation_type",
        "incomplete_trade_data",
        "inconsistent_listing",
    ]
    operation_type: Identifier | None  # as Horizon reports it, when the operation was found


class ClawbackContext(Contract):
    """A forced withdrawal: units leave the holder's balance under the
    asset's clawback authority. It is neither a delivery, a payment nor a redemption burn,
    and by itself it proves no breach of any operation.

    Four roles stay apart: the holder whose balance decreases is the effect's
    ``account`` (or its ``holder`` when it is not an account); ``issuer`` is the asset's
    issuer; the authority is the issuer for a Classic operation (``operation_source``, who
    submitted it, which the protocol requires to be the issuer) and the SAC admin for a
    contract call, which no SAC event names (``operation_source`` None); the counterparty
    is the issuer as the supply side, because clawed-back units leave circulation: it is
    neither a credit to the issuer's balance nor proof of who authorized it.

    ``operation_type`` is what Horizon reports for the Classic operation (None on a SAC
    event, which does not name it)."""

    asset: ChainAsset
    issuer: StellarAccountId
    operation_type: Literal["clawback"] | None
    operation_source: StellarAccountId | None

    @model_validator(mode="after")
    def _issuer(self) -> Self:
        if self.asset == "native" or self.asset.split(":")[1] != self.issuer:
            raise ValueError("a clawback withdraws a credit asset of the named issuer")
        if (self.operation_type is None) != (self.operation_source is None):
            raise ValueError("operation type and source are both known (Classic) or neither")
        return self


ClaimableBalanceOperation = Literal[
    "create_claimable_balance", "claim_claimable_balance", "clawback_claimable_balance"
]


class ClaimableBalanceContext(Contract):
    """A claimable balance's technical participants, as Horizon
    reports its history (``/claimable_balances/{id}/operations``).

    ``creator`` is the account whose balance was debited when the balance was created (the
    issuer when it was minted into it), or ``unresolved``. ``claimants`` may claim it: a
    claimant has received nothing until a claim is observed, and none of them is the owner
    of the account debited at creation. None means the history could not be read; the
    participants are then unknown and the relevance of the effect cannot be ruled out.
    ``operation_type`` and ``operation_source`` are those of this effect's operation as
    Horizon reports them (None when it could not be read)."""

    balance_id: Annotated[str, StringConstraints(pattern=r"^B[A-Z2-7]{57}$")]
    operation_type: ClaimableBalanceOperation | None
    operation_source: StellarAccountId | None
    creator: ChainParty
    claimants: Annotated[list[StellarAccountId], Field(min_length=1)] | None

    @model_validator(mode="after")
    def _participants(self) -> Self:
        decode_strkey("claimable_balance", self.balance_id)
        if self.creator.kind not in ("account", "unresolved"):
            raise ValueError("a claimable balance is created by an account")
        if (self.operation_type is None) != (self.operation_source is None):
            raise ValueError("operation type and source are both known or neither")
        return self


PATH_EFFECTS = frozenset({"path_payment_debit", "path_payment_credit", "path_payment_failed"})
# Effects on a typed non-account party, from SAC events.
TYPED_PARTY_EFFECTS = {
    "claimable_balance_created": ("claimable_balance", "debit"),
    "claimable_balance_claimed": ("claimable_balance", "credit"),
    "contract_transfer": ("contract", None),
    "pool_transfer": ("liquidity_pool", None),
}


class ChainEffectPayload(Contract):
    """One on-chain effect on the target asset, from one representation.

    Classic effects (debit, credit, failed): ``account`` is the base account whose balance
    of the target asset Horizon reports as debited or credited by the operation; they
    carry the operation context and the transaction outcome. A failed operation keeps its
    technical result, never an amount.

    SAC legs: ``account``/``counterparty`` are the parties of one CAP-67 event, seen from
    its sender (or from its receiver when the sender is not an account). A leg is evidence
    of the event, not of an effective balance change of ``account``: conversion legs of a
    path payment can pass through accounts whose Classic balance does not change.

    DEX fills (``dex_fill``): an offer of the watched account crossed, or the watched
    account's own offer operation crossing others. Classic fills come from Horizon trade
    effects (executed amounts of both assets, offer id); SAC fills from the CAP-67 event,
    seen from the watched account. ``unresolved_movement``: a SAC movement whose origin
    could not be established; it is kept, and it satisfies nothing.

    Typed parties (increment 3): a SAC movement between an account and a contract
    (``contract_transfer``), a liquidity pool (``pool_transfer``) or a claimable balance
    (``claimable_balance_created`` into it, ``claimable_balance_claimed`` out of it).
    ``account`` is the account side (the issuer for a mint or burn), ``counterparty`` the
    typed party. A clawback whose holder is not an account names it in ``holder`` and has
    no ``account``. None of them is evidence of fulfilment: a technically complete
    representation still proves no payment, delivery or redemption.
    """

    payload_type: Literal["chain_effect"]
    effect_kind: Literal[
        "path_payment_debit",
        "path_payment_credit",
        "path_payment_leg",
        "path_payment_failed",
        "dex_fill",
        "unresolved_movement",
        "clawback",
        "claimable_balance_created",
        "claimable_balance_claimed",
        "contract_transfer",
        "pool_transfer",
    ]
    representation: Literal["classic", "sac"]
    account: StellarAccountId | None
    direction: Literal["debit", "credit"] | None
    counterparty: ChainParty | None
    units: Quantity | None
    chain: ChainCoordinates
    path_payment: PathPaymentContext | None
    transaction: TransactionOutcome | None
    # Absent from the serialized form when None, so path payment effects stay identical.
    exchange: ExchangeContext | None = Field(default=None, exclude_if=_absent_if_none)
    unresolved: UnresolvedOrigin | None = Field(default=None, exclude_if=_absent_if_none)
    clawback: ClawbackContext | None = Field(default=None, exclude_if=_absent_if_none)
    holder: ChainParty | None = Field(default=None, exclude_if=_absent_if_none)
    claimable_balance: ClaimableBalanceContext | None = Field(
        default=None, exclude_if=_absent_if_none
    )

    @model_validator(mode="after")
    def _consistency(self) -> Self:
        kind = self.effect_kind
        failed = kind == "path_payment_failed"
        if failed != (self.units is None) or failed != (self.direction is None):
            raise ValueError("only a failed operation lacks units and direction")
        if failed and self.chain.tx_successful:
            raise ValueError("a failed effect must come from an unsuccessful transaction")
        if not failed and not self.chain.tx_successful:
            raise ValueError("an executed effect must come from a successful transaction")
        if kind == "path_payment_leg" and self.representation != "sac":
            raise ValueError("legs come from SAC events; debit, credit and failed from Classic")
        if kind in PATH_EFFECTS and self.representation != "classic":
            raise ValueError("legs come from SAC events; debit, credit and failed from Classic")
        if kind == "unresolved_movement" and self.representation != "sac":
            raise ValueError("an unresolved movement comes from a SAC event")
        if self.counterparty is None or self.counterparty.kind == "unresolved":
            raise ValueError("every chain effect names its counterparty")
        if (self.account is None) == (self.holder is None):
            raise ValueError("an effect names its account or, if it is not one, its holder")
        if self.holder is not None and (
            kind != "clawback" or self.holder.kind not in ("contract", "claimable_balance")
        ):
            raise ValueError("only a clawback names a non-account holder (contract or balance)")
        typed = TYPED_PARTY_EFFECTS.get(kind)
        if typed is not None:
            party_kind, direction = typed
            if self.representation != "sac" or self.counterparty.kind != party_kind:
                raise ValueError(f"a {kind} is a SAC event with a {party_kind} counterparty")
            if direction is not None and self.direction != direction:
                raise ValueError(f"a {kind} is a {direction} of the account")
            if self.units is None or int(self.units.atoms) <= 0:
                raise ValueError("a typed party movement moves a positive amount")
        balance = (
            self.counterparty
            if kind in ("claimable_balance_created", "claimable_balance_claimed")
            else self.holder
            if self.holder is not None and self.holder.kind == "claimable_balance"
            else None
        )
        if (balance is not None) != (self.claimable_balance is not None):
            raise ValueError("an effect on a claimable balance, and only it, carries its context")
        if balance is not None and self.claimable_balance is not None:
            if self.claimable_balance.balance_id != balance.id:
                raise ValueError("the claimable balance context names the effect's balance")
        path = kind in PATH_EFFECTS
        if path != (self.path_payment is not None) or path != (self.transaction is not None):
            raise ValueError("a Classic path effect carries its operation and transaction")
        if (kind == "dex_fill") != (self.exchange is not None):
            raise ValueError("a DEX fill, and only a DEX fill, carries its exchange context")
        if self.exchange is not None and (self.exchange.offer_id is not None) != (
            self.representation == "classic"
        ):
            raise ValueError("a Classic fill carries its trade; a SAC fill only the operation")
        if (kind == "unresolved_movement") != (self.unresolved is not None):
            raise ValueError("an unresolved movement, and only it, says why")
        if (kind == "clawback") != (self.clawback is not None):
            raise ValueError("a clawback, and only a clawback, carries its clawback context")
        if self.clawback is not None:
            if self.direction != "debit":
                raise ValueError("a clawback debits the affected account")
            if self.counterparty != ChainParty(kind="account", id=self.clawback.issuer):
                raise ValueError("the counterparty of a clawback is the asset's issuer")
            if (self.clawback.operation_type is not None) != (self.representation == "classic"):
                raise ValueError("only the Classic side names the clawback operation")
            if self.units is None or int(self.units.atoms) <= 0:
                raise ValueError("a clawback withdraws a positive amount")
        if self.units is not None and self.units.scale != 7:
            raise ValueError("Stellar amounts are at stroop scale (7)")
        return self


def effect_parties(payload: ChainEffectPayload) -> list[ChainParty]:
    """Every technical participant a chain effect names: its account (or non-account
    holder), its counterparty, a path payment's source and destination, who submitted a DEX
    operation, and a claimable balance's creator and claimants. Scope for relevance, never
    attribution of funds or ownership. Unresolved participants are included as such."""
    found: list[ChainParty] = []

    def add(party: ChainParty) -> None:
        if party not in found:
            found.append(party)

    def account(account_id: str) -> None:
        add(ChainParty(kind="account", id=account_id))

    if payload.account is not None:
        account(payload.account)
    if payload.holder is not None:
        add(payload.holder)
    if payload.counterparty is not None:
        add(payload.counterparty)
    if payload.path_payment is not None:
        account(payload.path_payment.source_account)
        account(payload.path_payment.destination_account)
    if payload.exchange is not None:
        account(payload.exchange.operation_source)
    if payload.claimable_balance is not None:
        add(payload.claimable_balance.creator)
        for claimant in payload.claimable_balance.claimants or ():
            account(claimant)
        if payload.claimable_balance.claimants is None:
            add(ChainParty(kind="unresolved", reason="claimants of the claimable balance"))
    return found


Payload = Annotated[
    OrderPayload
    | CashPayload
    | UnitsPayload
    | TokenMovementPayload
    | RedemptionRequestPayload
    | RedemptionCancellationPayload
    | PositionPayload
    | PricePayload
    | PositionChangePayload
    | RedemptionReactivationPayload
    | ChainEffectPayload,
    Field(discriminator="payload_type"),
]


class Observation(Contract):
    schema_version: SchemaVersion
    observation_id: Identifier
    tenant_id: Identifier
    kind: Literal["assertion", "retraction"]
    fact_type: FactType
    instrument_id: Identifier
    representation_id: Identifier | None
    operation_ref: Identifier | None
    source: SourceRecord
    valid_time: UtcDatetime
    recorded_at: UtcDatetime
    provenance: Provenance
    supersedes: Identifier | None
    payload: Payload | None
    synthetic: bool

    @model_validator(mode="after")
    def _consistency(self) -> Self:
        if self.kind == "retraction":
            if self.payload is not None:
                raise ValueError("a retraction carries no payload")
            if self.supersedes is None:
                raise ValueError("a retraction must name the revision it withdraws")
        else:
            if self.payload is None:
                raise ValueError("an assertion requires a payload")
            if self.payload.payload_type != self.fact_type:
                raise ValueError("payload_type must equal fact_type")
        if self.supersedes == self.observation_id:
            raise ValueError("an observation cannot supersede itself")
        if self.supersedes is not None and self.source.revision < 2:
            raise ValueError("a superseding observation must have revision >= 2")
        if self.fact_type == "token_movement" and self.representation_id is None:
            raise ValueError("token_movement requires representation_id")
        return self
