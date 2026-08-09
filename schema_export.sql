--
-- PostgreSQL database dump
--

\restrict 2ACnGTjQlgYQGJ2JaiLrgH9tnw5hMq7BdPSgpd5Tg5U0skGMeDPhMdaWz1fKBzS

-- Dumped from database version 16.14 (Ubuntu 16.14-0ubuntu0.24.04.1)
-- Dumped by pg_dump version 16.14 (Ubuntu 16.14-0ubuntu0.24.04.1)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: rental_core; Type: SCHEMA; Schema: -; Owner: postgres
--

CREATE SCHEMA rental_core;


ALTER SCHEMA rental_core OWNER TO postgres;

SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: gas_bill_master; Type: TABLE; Schema: rental_core; Owner: postgres
--

CREATE TABLE rental_core.gas_bill_master (
    id integer NOT NULL,
    customer_number character varying(50) NOT NULL,
    amount numeric(10,2) DEFAULT 0.00 NOT NULL,
    partner_name character varying(50) DEFAULT 'GUJARAT_GAS'::character varying NOT NULL,
    bill_type character varying(50) DEFAULT 'NORMAL'::character varying NOT NULL,
    last_pay_date date,
    execution_month character varying(7) NOT NULL,
    created_at timestamp without time zone DEFAULT now()
);


ALTER TABLE rental_core.gas_bill_master OWNER TO postgres;

--
-- Name: gas_bill_master_id_seq; Type: SEQUENCE; Schema: rental_core; Owner: postgres
--

CREATE SEQUENCE rental_core.gas_bill_master_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE rental_core.gas_bill_master_id_seq OWNER TO postgres;

--
-- Name: gas_bill_master_id_seq; Type: SEQUENCE OWNED BY; Schema: rental_core; Owner: postgres
--

ALTER SEQUENCE rental_core.gas_bill_master_id_seq OWNED BY rental_core.gas_bill_master.id;


--
-- Name: gas_metrics; Type: TABLE; Schema: rental_core; Owner: postgres
--

CREATE TABLE rental_core.gas_metrics (
    id integer NOT NULL,
    room_id integer,
    customer_number character varying(50) NOT NULL,
    created_at timestamp without time zone DEFAULT now()
);


ALTER TABLE rental_core.gas_metrics OWNER TO postgres;

--
-- Name: gas_metrics_id_seq; Type: SEQUENCE; Schema: rental_core; Owner: postgres
--

CREATE SEQUENCE rental_core.gas_metrics_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE rental_core.gas_metrics_id_seq OWNER TO postgres;

--
-- Name: gas_metrics_id_seq; Type: SEQUENCE OWNED BY; Schema: rental_core; Owner: postgres
--

ALTER SEQUENCE rental_core.gas_metrics_id_seq OWNED BY rental_core.gas_metrics.id;


--
-- Name: monthly_ledgers; Type: TABLE; Schema: rental_core; Owner: platform_admin
--

CREATE TABLE rental_core.monthly_ledgers (
    id integer NOT NULL,
    room_id integer,
    tenant_id integer,
    billing_month character varying(20) NOT NULL,
    prev_baki numeric(10,2) DEFAULT 0.00,
    prev_jama numeric(10,2) DEFAULT 0.00,
    bhadu_applied numeric(10,2) NOT NULL,
    light_bill numeric(10,2) DEFAULT 0.00,
    gas_bill numeric(10,2) DEFAULT 0.00,
    maintenance_applied numeric(10,2) DEFAULT 0.00,
    jama numeric(10,2) DEFAULT 0.00,
    jama_date character varying(20) DEFAULT '0'::character varying,
    payment_status character varying(20) DEFAULT 'Unpaid'::character varying,
    light_bill_type character varying(20) DEFAULT 'NORMAL'::character varying,
    common_misc_expenses numeric(10,2) DEFAULT 0.00,
    month_total numeric(10,2) GENERATED ALWAYS AS (((((bhadu_applied + light_bill) + gas_bill) + maintenance_applied) + common_misc_expenses)) STORED,
    gross_baki numeric(10,2) GENERATED ALWAYS AS (((((((prev_baki + bhadu_applied) + light_bill) + gas_bill) + maintenance_applied) + common_misc_expenses) - prev_jama)) STORED,
    new_baki numeric(10,2) GENERATED ALWAYS AS ((((((((prev_baki + bhadu_applied) + light_bill) + gas_bill) + maintenance_applied) + common_misc_expenses) - prev_jama) - jama)) STORED
);


ALTER TABLE rental_core.monthly_ledgers OWNER TO platform_admin;

--
-- Name: monthly_ledgers_id_seq; Type: SEQUENCE; Schema: rental_core; Owner: platform_admin
--

CREATE SEQUENCE rental_core.monthly_ledgers_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE rental_core.monthly_ledgers_id_seq OWNER TO platform_admin;

--
-- Name: monthly_ledgers_id_seq; Type: SEQUENCE OWNED BY; Schema: rental_core; Owner: platform_admin
--

ALTER SEQUENCE rental_core.monthly_ledgers_id_seq OWNED BY rental_core.monthly_ledgers.id;


--
-- Name: tenant_payments; Type: TABLE; Schema: rental_core; Owner: platform_admin
--

CREATE TABLE rental_core.tenant_payments (
    id integer NOT NULL,
    tenant_id integer,
    room_id integer,
    billing_month character varying(20) NOT NULL,
    payment_date date NOT NULL,
    amount numeric(10,2) NOT NULL,
    payment_mode character varying(20) DEFAULT 'CASH'::character varying NOT NULL,
    payment_type character varying(20) DEFAULT 'RENT'::character varying NOT NULL,
    note text,
    created_at timestamp without time zone DEFAULT now(),
    CONSTRAINT tenant_payments_payment_mode_check CHECK (((payment_mode)::text = ANY (ARRAY[('CASH'::character varying)::text, ('UPI'::character varying)::text, ('BANK_TRANSFER'::character varying)::text, ('CARD'::character varying)::text, ('CHEQUE'::character varying)::text]))),
    CONSTRAINT tenant_payments_payment_type_check CHECK (((payment_type)::text = ANY (ARRAY[('RENT'::character varying)::text, ('DEPOSIT'::character varying)::text])))
);


ALTER TABLE rental_core.tenant_payments OWNER TO platform_admin;

--
-- Name: tenant_payments_id_seq; Type: SEQUENCE; Schema: rental_core; Owner: platform_admin
--

CREATE SEQUENCE rental_core.tenant_payments_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE rental_core.tenant_payments_id_seq OWNER TO platform_admin;

--
-- Name: tenant_payments_id_seq; Type: SEQUENCE OWNED BY; Schema: rental_core; Owner: platform_admin
--

ALTER SEQUENCE rental_core.tenant_payments_id_seq OWNED BY rental_core.tenant_payments.id;

--
-- Name: tenant_payments id; Type: DEFAULT; Schema: rental_core; Owner: platform_admin
--

ALTER TABLE ONLY rental_core.tenant_payments ALTER COLUMN id SET DEFAULT nextval('rental_core.tenant_payments_id_seq'::regclass);

--
-- Name: tenant_payments tenant_payments_pkey; Type: CONSTRAINT; Schema: rental_core; Owner: platform_admin
--

ALTER TABLE ONLY rental_core.tenant_payments
    ADD CONSTRAINT tenant_payments_pkey PRIMARY KEY (id);

--
-- Name: tenant_payments tenant_payments_room_id_fkey; Type: FK CONSTRAINT; Schema: rental_core; Owner: platform_admin
--

ALTER TABLE ONLY rental_core.tenant_payments
    ADD CONSTRAINT tenant_payments_room_id_fkey FOREIGN KEY (room_id) REFERENCES rental_core.rooms(id) ON DELETE SET NULL;

--
-- Name: tenant_payments tenant_payments_tenant_id_fkey; Type: FK CONSTRAINT; Schema: rental_core; Owner: platform_admin
--

ALTER TABLE ONLY rental_core.tenant_payments
    ADD CONSTRAINT tenant_payments_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES rental_core.tenants(id) ON DELETE SET NULL;


--
-- Name: property_maintenance_expenses; Type: TABLE; Schema: rental_core; Owner: postgres
--

CREATE TABLE rental_core.property_maintenance_expenses (
    id integer NOT NULL,
    master_id integer,
    expense_name character varying(200) NOT NULL,
    amount numeric(10,2) NOT NULL,
    execution_month character varying(7) NOT NULL,
    created_at timestamp without time zone DEFAULT now(),
    charge_type character varying(20) DEFAULT 'SHARED'::character varying NOT NULL,
    room_id integer,
    CONSTRAINT property_maintenance_expenses_charge_type_check CHECK (((charge_type)::text = ANY ((ARRAY['SHARED'::character varying, 'UNIT'::character varying, 'OWNER'::character varying])::text[])))
);


ALTER TABLE rental_core.property_maintenance_expenses OWNER TO postgres;

--
-- Name: property_maintenance_expenses_id_seq; Type: SEQUENCE; Schema: rental_core; Owner: postgres
--

CREATE SEQUENCE rental_core.property_maintenance_expenses_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE rental_core.property_maintenance_expenses_id_seq OWNER TO postgres;

--
-- Name: property_maintenance_expenses_id_seq; Type: SEQUENCE OWNED BY; Schema: rental_core; Owner: postgres
--

ALTER SEQUENCE rental_core.property_maintenance_expenses_id_seq OWNED BY rental_core.property_maintenance_expenses.id;


--
-- Name: property_masters; Type: TABLE; Schema: rental_core; Owner: platform_admin
--

CREATE TABLE rental_core.property_masters (
    id integer NOT NULL,
    apartment_name character varying(100) NOT NULL,
    plot_number character varying(50) NOT NULL,
    default_base_rent numeric(10,2) DEFAULT 3300.00 NOT NULL,
    default_maintenance_fee numeric(10,2) DEFAULT 0.00 NOT NULL,
    default_security_deposit numeric(10,2) DEFAULT 5000.00 NOT NULL,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    default_common_utility numeric(10,2) DEFAULT 0.00
);


ALTER TABLE rental_core.property_masters OWNER TO platform_admin;

--
-- Name: property_masters_id_seq; Type: SEQUENCE; Schema: rental_core; Owner: platform_admin
--

CREATE SEQUENCE rental_core.property_masters_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE rental_core.property_masters_id_seq OWNER TO platform_admin;

--
-- Name: property_masters_id_seq; Type: SEQUENCE OWNED BY; Schema: rental_core; Owner: platform_admin
--

ALTER SEQUENCE rental_core.property_masters_id_seq OWNED BY rental_core.property_masters.id;


--
-- Name: rooms; Type: TABLE; Schema: rental_core; Owner: platform_admin
--

CREATE TABLE rental_core.rooms (
    id integer NOT NULL,
    master_id integer,
    flat_number character varying(50) NOT NULL,
    display_address character varying(100) NOT NULL
);


ALTER TABLE rental_core.rooms OWNER TO platform_admin;

--
-- Name: rooms_id_seq; Type: SEQUENCE; Schema: rental_core; Owner: platform_admin
--

CREATE SEQUENCE rental_core.rooms_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE rental_core.rooms_id_seq OWNER TO platform_admin;

--
-- Name: rooms_id_seq; Type: SEQUENCE OWNED BY; Schema: rental_core; Owner: platform_admin
--

ALTER SEQUENCE rental_core.rooms_id_seq OWNED BY rental_core.rooms.id;


--
-- Name: tenant_ledgers; Type: TABLE; Schema: rental_core; Owner: postgres
--

CREATE TABLE rental_core.tenant_ledgers (
    id integer NOT NULL,
    room_id integer,
    execution_month character varying(7) NOT NULL,
    rent_amount numeric(10,2) DEFAULT 5000.00,
    electricity_amount numeric(10,2) DEFAULT 0.00,
    gas_amount numeric(10,2) DEFAULT 0.00,
    common_utility_share numeric(10,2) DEFAULT 0.00,
    total_due numeric(10,2) DEFAULT 0.00,
    payment_status character varying(20) DEFAULT 'UNPAID'::character varying,
    created_at timestamp without time zone DEFAULT now()
);


ALTER TABLE rental_core.tenant_ledgers OWNER TO postgres;

--
-- Name: tenant_ledgers_id_seq; Type: SEQUENCE; Schema: rental_core; Owner: postgres
--

CREATE SEQUENCE rental_core.tenant_ledgers_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE rental_core.tenant_ledgers_id_seq OWNER TO postgres;

--
-- Name: tenant_ledgers_id_seq; Type: SEQUENCE OWNED BY; Schema: rental_core; Owner: postgres
--

ALTER SEQUENCE rental_core.tenant_ledgers_id_seq OWNED BY rental_core.tenant_ledgers.id;


--
-- Name: tenants; Type: TABLE; Schema: rental_core; Owner: platform_admin
--

CREATE TABLE rental_core.tenants (
    id integer NOT NULL,
    room_id integer,
    tenant_name character varying(100) NOT NULL,
    phone_number character varying(50) NOT NULL,
    is_active character(1) DEFAULT 'Y'::bpchar,
    base_rent_applied numeric(10,2) NOT NULL,
    maintenance_applied numeric(10,2) NOT NULL,
    security_deposit_applied numeric(10,2) NOT NULL,
    lease_start_date date DEFAULT CURRENT_DATE,
    lease_end_date date
);


ALTER TABLE rental_core.tenants OWNER TO platform_admin;

--
-- Name: tenants_id_seq; Type: SEQUENCE; Schema: rental_core; Owner: platform_admin
--

CREATE SEQUENCE rental_core.tenants_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE rental_core.tenants_id_seq OWNER TO platform_admin;

--
-- Name: tenants_id_seq; Type: SEQUENCE OWNED BY; Schema: rental_core; Owner: platform_admin
--

ALTER SEQUENCE rental_core.tenants_id_seq OWNED BY rental_core.tenants.id;


--
-- Name: torrent_accounts; Type: TABLE; Schema: rental_core; Owner: postgres
--

CREATE TABLE rental_core.torrent_accounts (
    id integer NOT NULL,
    account_name character varying(50),
    encrypted_login_payload text,
    last_login_token text,
    password character varying(100),
    login_service_number character varying(50),
    access_token text
);


ALTER TABLE rental_core.torrent_accounts OWNER TO postgres;

--
-- Name: torrent_accounts_id_seq; Type: SEQUENCE; Schema: rental_core; Owner: postgres
--

CREATE SEQUENCE rental_core.torrent_accounts_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE rental_core.torrent_accounts_id_seq OWNER TO postgres;

--
-- Name: torrent_accounts_id_seq; Type: SEQUENCE OWNED BY; Schema: rental_core; Owner: postgres
--

ALTER SEQUENCE rental_core.torrent_accounts_id_seq OWNED BY rental_core.torrent_accounts.id;


--
-- Name: torrent_bill_master; Type: TABLE; Schema: rental_core; Owner: postgres
--

CREATE TABLE rental_core.torrent_bill_master (
    id integer NOT NULL,
    service_number character varying(50),
    amount numeric(10,2),
    partner_name character varying(100),
    bill_type character varying(20),
    last_pay_date timestamp without time zone,
    execution_month character varying(7),
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP
);


ALTER TABLE rental_core.torrent_bill_master OWNER TO postgres;

--
-- Name: torrent_bill_master_id_seq; Type: SEQUENCE; Schema: rental_core; Owner: postgres
--

CREATE SEQUENCE rental_core.torrent_bill_master_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE rental_core.torrent_bill_master_id_seq OWNER TO postgres;

--
-- Name: torrent_bill_master_id_seq; Type: SEQUENCE OWNED BY; Schema: rental_core; Owner: postgres
--

ALTER SEQUENCE rental_core.torrent_bill_master_id_seq OWNED BY rental_core.torrent_bill_master.id;


--
-- Name: utility_metrics; Type: TABLE; Schema: rental_core; Owner: platform_admin
--

CREATE TABLE rental_core.utility_metrics (
    id integer NOT NULL,
    room_id integer,
    service_number character varying(50) NOT NULL,
    account_id integer,
    encrypted_value text
);


ALTER TABLE rental_core.utility_metrics OWNER TO platform_admin;

--
-- Name: utility_metrics_id_seq; Type: SEQUENCE; Schema: rental_core; Owner: platform_admin
--

CREATE SEQUENCE rental_core.utility_metrics_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE rental_core.utility_metrics_id_seq OWNER TO platform_admin;

--
-- Name: utility_metrics_id_seq; Type: SEQUENCE OWNED BY; Schema: rental_core; Owner: platform_admin
--

ALTER SEQUENCE rental_core.utility_metrics_id_seq OWNED BY rental_core.utility_metrics.id;


--
-- Name: gas_bill_master id; Type: DEFAULT; Schema: rental_core; Owner: postgres
--

ALTER TABLE ONLY rental_core.gas_bill_master ALTER COLUMN id SET DEFAULT nextval('rental_core.gas_bill_master_id_seq'::regclass);


--
-- Name: gas_metrics id; Type: DEFAULT; Schema: rental_core; Owner: postgres
--

ALTER TABLE ONLY rental_core.gas_metrics ALTER COLUMN id SET DEFAULT nextval('rental_core.gas_metrics_id_seq'::regclass);


--
-- Name: monthly_ledgers id; Type: DEFAULT; Schema: rental_core; Owner: platform_admin
--

ALTER TABLE ONLY rental_core.monthly_ledgers ALTER COLUMN id SET DEFAULT nextval('rental_core.monthly_ledgers_id_seq'::regclass);


--
-- Name: property_maintenance_expenses id; Type: DEFAULT; Schema: rental_core; Owner: postgres
--

ALTER TABLE ONLY rental_core.property_maintenance_expenses ALTER COLUMN id SET DEFAULT nextval('rental_core.property_maintenance_expenses_id_seq'::regclass);


--
-- Name: property_masters id; Type: DEFAULT; Schema: rental_core; Owner: platform_admin
--

ALTER TABLE ONLY rental_core.property_masters ALTER COLUMN id SET DEFAULT nextval('rental_core.property_masters_id_seq'::regclass);


--
-- Name: rooms id; Type: DEFAULT; Schema: rental_core; Owner: platform_admin
--

ALTER TABLE ONLY rental_core.rooms ALTER COLUMN id SET DEFAULT nextval('rental_core.rooms_id_seq'::regclass);


--
-- Name: tenant_ledgers id; Type: DEFAULT; Schema: rental_core; Owner: postgres
--

ALTER TABLE ONLY rental_core.tenant_ledgers ALTER COLUMN id SET DEFAULT nextval('rental_core.tenant_ledgers_id_seq'::regclass);


--
-- Name: tenants id; Type: DEFAULT; Schema: rental_core; Owner: platform_admin
--

ALTER TABLE ONLY rental_core.tenants ALTER COLUMN id SET DEFAULT nextval('rental_core.tenants_id_seq'::regclass);


--
-- Name: torrent_accounts id; Type: DEFAULT; Schema: rental_core; Owner: postgres
--

ALTER TABLE ONLY rental_core.torrent_accounts ALTER COLUMN id SET DEFAULT nextval('rental_core.torrent_accounts_id_seq'::regclass);


--
-- Name: torrent_bill_master id; Type: DEFAULT; Schema: rental_core; Owner: postgres
--

ALTER TABLE ONLY rental_core.torrent_bill_master ALTER COLUMN id SET DEFAULT nextval('rental_core.torrent_bill_master_id_seq'::regclass);


--
-- Name: utility_metrics id; Type: DEFAULT; Schema: rental_core; Owner: platform_admin
--

ALTER TABLE ONLY rental_core.utility_metrics ALTER COLUMN id SET DEFAULT nextval('rental_core.utility_metrics_id_seq'::regclass);


--
-- Name: gas_bill_master gas_bill_master_pkey; Type: CONSTRAINT; Schema: rental_core; Owner: postgres
--

ALTER TABLE ONLY rental_core.gas_bill_master
    ADD CONSTRAINT gas_bill_master_pkey PRIMARY KEY (id);


--
-- Name: gas_metrics gas_metrics_pkey; Type: CONSTRAINT; Schema: rental_core; Owner: postgres
--

ALTER TABLE ONLY rental_core.gas_metrics
    ADD CONSTRAINT gas_metrics_pkey PRIMARY KEY (id);


--
-- Name: monthly_ledgers monthly_ledgers_pkey; Type: CONSTRAINT; Schema: rental_core; Owner: platform_admin
--

ALTER TABLE ONLY rental_core.monthly_ledgers
    ADD CONSTRAINT monthly_ledgers_pkey PRIMARY KEY (id);


--
-- Name: property_maintenance_expenses property_maintenance_expenses_pkey; Type: CONSTRAINT; Schema: rental_core; Owner: postgres
--

ALTER TABLE ONLY rental_core.property_maintenance_expenses
    ADD CONSTRAINT property_maintenance_expenses_pkey PRIMARY KEY (id);


--
-- Name: property_masters property_masters_pkey; Type: CONSTRAINT; Schema: rental_core; Owner: platform_admin
--

ALTER TABLE ONLY rental_core.property_masters
    ADD CONSTRAINT property_masters_pkey PRIMARY KEY (id);


--
-- Name: property_masters property_masters_plot_number_key; Type: CONSTRAINT; Schema: rental_core; Owner: platform_admin
--

ALTER TABLE ONLY rental_core.property_masters
    ADD CONSTRAINT property_masters_plot_number_key UNIQUE (plot_number);


--
-- Name: rooms rooms_display_address_key; Type: CONSTRAINT; Schema: rental_core; Owner: platform_admin
--

ALTER TABLE ONLY rental_core.rooms
    ADD CONSTRAINT rooms_display_address_key UNIQUE (display_address);


--
-- Name: rooms rooms_pkey; Type: CONSTRAINT; Schema: rental_core; Owner: platform_admin
--

ALTER TABLE ONLY rental_core.rooms
    ADD CONSTRAINT rooms_pkey PRIMARY KEY (id);


--
-- Name: tenant_ledgers tenant_ledgers_pkey; Type: CONSTRAINT; Schema: rental_core; Owner: postgres
--

ALTER TABLE ONLY rental_core.tenant_ledgers
    ADD CONSTRAINT tenant_ledgers_pkey PRIMARY KEY (id);


--
-- Name: tenants tenants_pkey; Type: CONSTRAINT; Schema: rental_core; Owner: platform_admin
--

ALTER TABLE ONLY rental_core.tenants
    ADD CONSTRAINT tenants_pkey PRIMARY KEY (id);


--
-- Name: torrent_accounts torrent_accounts_pkey; Type: CONSTRAINT; Schema: rental_core; Owner: postgres
--

ALTER TABLE ONLY rental_core.torrent_accounts
    ADD CONSTRAINT torrent_accounts_pkey PRIMARY KEY (id);


--
-- Name: torrent_bill_master torrent_bill_master_pkey; Type: CONSTRAINT; Schema: rental_core; Owner: postgres
--

ALTER TABLE ONLY rental_core.torrent_bill_master
    ADD CONSTRAINT torrent_bill_master_pkey PRIMARY KEY (id);


--
-- Name: gas_bill_master unique_gas_bill_month; Type: CONSTRAINT; Schema: rental_core; Owner: postgres
--

ALTER TABLE ONLY rental_core.gas_bill_master
    ADD CONSTRAINT unique_gas_bill_month UNIQUE (customer_number, execution_month);


--
-- Name: gas_metrics unique_gas_customer; Type: CONSTRAINT; Schema: rental_core; Owner: postgres
--

ALTER TABLE ONLY rental_core.gas_metrics
    ADD CONSTRAINT unique_gas_customer UNIQUE (customer_number);


--
-- Name: tenant_ledgers unique_room_month; Type: CONSTRAINT; Schema: rental_core; Owner: postgres
--

ALTER TABLE ONLY rental_core.tenant_ledgers
    ADD CONSTRAINT unique_room_month UNIQUE (room_id, execution_month);


--
-- Name: torrent_bill_master unique_service_month; Type: CONSTRAINT; Schema: rental_core; Owner: postgres
--

ALTER TABLE ONLY rental_core.torrent_bill_master
    ADD CONSTRAINT unique_service_month UNIQUE (service_number, execution_month);


--
-- Name: utility_metrics unique_service_number; Type: CONSTRAINT; Schema: rental_core; Owner: platform_admin
--

ALTER TABLE ONLY rental_core.utility_metrics
    ADD CONSTRAINT unique_service_number UNIQUE (service_number);


--
-- Name: monthly_ledgers unique_tenant_month; Type: CONSTRAINT; Schema: rental_core; Owner: platform_admin
--

ALTER TABLE ONLY rental_core.monthly_ledgers
    ADD CONSTRAINT unique_tenant_month UNIQUE (tenant_id, billing_month);


--
-- Name: utility_metrics utility_metrics_pkey; Type: CONSTRAINT; Schema: rental_core; Owner: platform_admin
--

ALTER TABLE ONLY rental_core.utility_metrics
    ADD CONSTRAINT utility_metrics_pkey PRIMARY KEY (id);


--
-- Name: utility_metrics utility_metrics_service_number_key; Type: CONSTRAINT; Schema: rental_core; Owner: platform_admin
--

ALTER TABLE ONLY rental_core.utility_metrics
    ADD CONSTRAINT utility_metrics_service_number_key UNIQUE (service_number);


--
-- Name: gas_bill_master gas_bill_master_customer_number_fkey; Type: FK CONSTRAINT; Schema: rental_core; Owner: postgres
--

ALTER TABLE ONLY rental_core.gas_bill_master
    ADD CONSTRAINT gas_bill_master_customer_number_fkey FOREIGN KEY (customer_number) REFERENCES rental_core.gas_metrics(customer_number) ON DELETE CASCADE;


--
-- Name: gas_metrics gas_metrics_room_id_fkey; Type: FK CONSTRAINT; Schema: rental_core; Owner: postgres
--

ALTER TABLE ONLY rental_core.gas_metrics
    ADD CONSTRAINT gas_metrics_room_id_fkey FOREIGN KEY (room_id) REFERENCES rental_core.rooms(id) ON DELETE CASCADE;


--
-- Name: monthly_ledgers monthly_ledgers_room_id_fkey; Type: FK CONSTRAINT; Schema: rental_core; Owner: platform_admin
--

ALTER TABLE ONLY rental_core.monthly_ledgers
    ADD CONSTRAINT monthly_ledgers_room_id_fkey FOREIGN KEY (room_id) REFERENCES rental_core.rooms(id) ON DELETE SET NULL;


--
-- Name: monthly_ledgers monthly_ledgers_tenant_id_fkey; Type: FK CONSTRAINT; Schema: rental_core; Owner: platform_admin
--

ALTER TABLE ONLY rental_core.monthly_ledgers
    ADD CONSTRAINT monthly_ledgers_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES rental_core.tenants(id) ON DELETE SET NULL;


--
-- Name: property_maintenance_expenses property_maintenance_expenses_master_id_fkey; Type: FK CONSTRAINT; Schema: rental_core; Owner: postgres
--

ALTER TABLE ONLY rental_core.property_maintenance_expenses
    ADD CONSTRAINT property_maintenance_expenses_master_id_fkey FOREIGN KEY (master_id) REFERENCES rental_core.property_masters(id) ON DELETE CASCADE;


--
-- Name: rooms rooms_master_id_fkey; Type: FK CONSTRAINT; Schema: rental_core; Owner: platform_admin
--

ALTER TABLE ONLY rental_core.rooms
    ADD CONSTRAINT rooms_master_id_fkey FOREIGN KEY (master_id) REFERENCES rental_core.property_masters(id) ON DELETE CASCADE;


--
-- Name: tenant_ledgers tenant_ledgers_room_id_fkey; Type: FK CONSTRAINT; Schema: rental_core; Owner: postgres
--

ALTER TABLE ONLY rental_core.tenant_ledgers
    ADD CONSTRAINT tenant_ledgers_room_id_fkey FOREIGN KEY (room_id) REFERENCES rental_core.rooms(id) ON DELETE CASCADE;


--
-- Name: tenants tenants_room_id_fkey; Type: FK CONSTRAINT; Schema: rental_core; Owner: platform_admin
--

ALTER TABLE ONLY rental_core.tenants
    ADD CONSTRAINT tenants_room_id_fkey FOREIGN KEY (room_id) REFERENCES rental_core.rooms(id) ON DELETE SET NULL;


--
-- Name: utility_metrics utility_metrics_account_id_fkey; Type: FK CONSTRAINT; Schema: rental_core; Owner: platform_admin
--

ALTER TABLE ONLY rental_core.utility_metrics
    ADD CONSTRAINT utility_metrics_account_id_fkey FOREIGN KEY (account_id) REFERENCES rental_core.torrent_accounts(id);


--
-- Name: utility_metrics utility_metrics_room_id_fkey; Type: FK CONSTRAINT; Schema: rental_core; Owner: platform_admin
--

ALTER TABLE ONLY rental_core.utility_metrics
    ADD CONSTRAINT utility_metrics_room_id_fkey FOREIGN KEY (room_id) REFERENCES rental_core.rooms(id) ON DELETE CASCADE;


--
-- Name: SCHEMA rental_core; Type: ACL; Schema: -; Owner: postgres
--

GRANT ALL ON SCHEMA rental_core TO platform_admin;


--
-- Name: TABLE gas_bill_master; Type: ACL; Schema: rental_core; Owner: postgres
--

GRANT ALL ON TABLE rental_core.gas_bill_master TO platform_admin;


--
-- Name: SEQUENCE gas_bill_master_id_seq; Type: ACL; Schema: rental_core; Owner: postgres
--

GRANT ALL ON SEQUENCE rental_core.gas_bill_master_id_seq TO platform_admin;


--
-- Name: TABLE gas_metrics; Type: ACL; Schema: rental_core; Owner: postgres
--

GRANT ALL ON TABLE rental_core.gas_metrics TO platform_admin;


--
-- Name: SEQUENCE gas_metrics_id_seq; Type: ACL; Schema: rental_core; Owner: postgres
--

GRANT ALL ON SEQUENCE rental_core.gas_metrics_id_seq TO platform_admin;


--
-- Name: TABLE property_maintenance_expenses; Type: ACL; Schema: rental_core; Owner: postgres
--

GRANT ALL ON TABLE rental_core.property_maintenance_expenses TO platform_admin;


--
-- Name: SEQUENCE property_maintenance_expenses_id_seq; Type: ACL; Schema: rental_core; Owner: postgres
--

GRANT ALL ON SEQUENCE rental_core.property_maintenance_expenses_id_seq TO platform_admin;


--
-- Name: TABLE tenant_ledgers; Type: ACL; Schema: rental_core; Owner: postgres
--

GRANT ALL ON TABLE rental_core.tenant_ledgers TO platform_admin;


--
-- Name: SEQUENCE tenant_ledgers_id_seq; Type: ACL; Schema: rental_core; Owner: postgres
--

GRANT ALL ON SEQUENCE rental_core.tenant_ledgers_id_seq TO platform_admin;


--
-- Name: TABLE torrent_accounts; Type: ACL; Schema: rental_core; Owner: postgres
--

GRANT ALL ON TABLE rental_core.torrent_accounts TO platform_admin;


--
-- Name: SEQUENCE torrent_accounts_id_seq; Type: ACL; Schema: rental_core; Owner: postgres
--

GRANT ALL ON SEQUENCE rental_core.torrent_accounts_id_seq TO platform_admin;


--
-- Name: TABLE torrent_bill_master; Type: ACL; Schema: rental_core; Owner: postgres
--

GRANT ALL ON TABLE rental_core.torrent_bill_master TO platform_admin;


--
-- Name: SEQUENCE torrent_bill_master_id_seq; Type: ACL; Schema: rental_core; Owner: postgres
--

GRANT ALL ON SEQUENCE rental_core.torrent_bill_master_id_seq TO platform_admin;


--
-- Name: DEFAULT PRIVILEGES FOR SEQUENCES; Type: DEFAULT ACL; Schema: rental_core; Owner: postgres
--

ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA rental_core GRANT ALL ON SEQUENCES TO platform_admin;


--
-- Name: DEFAULT PRIVILEGES FOR TABLES; Type: DEFAULT ACL; Schema: rental_core; Owner: postgres
--

ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA rental_core GRANT ALL ON TABLES TO platform_admin;


--
-- PostgreSQL database dump complete
--

\unrestrict 2ACnGTjQlgYQGJ2JaiLrgH9tnw5hMq7BdPSgpd5Tg5U0skGMeDPhMdaWz1fKBzS

