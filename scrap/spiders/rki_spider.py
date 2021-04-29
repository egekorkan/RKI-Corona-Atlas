import locale
import re
from datetime import datetime as dt
from pathlib import Path

import pycountry
import gettext

import scrapy
import pandas as pd

locale.setlocale(locale.LC_TIME, "de_DE.UTF-8")

data_dir = Path("assets/data")
db_path = data_dir/"db_scraped.csv"
date_path = data_dir/"report_date.csv"

de = gettext.translation('iso3166', pycountry.LOCALES_DIR, languages=['de'])


class RKISpider(scrapy.Spider):
    name = "rki"
    handle_httpstatus_list = [200, 404, 500]

    alias = {'BLR': ('Belarus',),
             'COD': ('Kongo DR',),
             'COG': ('Kongo Rep',),
             'CZE': ('Tschechien',),
             'MKD': ('Nordmazedonien',),
             'PRK': ('Korea (Volksrepublik)',),
             'PSE': ('Palästinensische Gebiete',),
             'SUR': ('Surinam',),
             'SYR': ('Syrische Arabische Republik',),
             'TLS': ('Timor Leste',),
             'TTO': ('Trinidad Tobago',),
             'VAT': ('Vatikanstadt',),
             'USA': ('USA ', ' USA')}

    date_fmt = {'db': '%Y-%m-%d', 'de': {'dt': '%d.%m.%Y', 're': r'\d{1,2}\.\d{1,2}\.\d{4}'},
                'risk': {'dt': '%d. %B %Y', 're': r'\d{1,2}\. +[äa-z]+ +\d{4}',
                         'fallback': '%d. %b %Y'}}

    h2_xpath = "//div[contains(@class, 'text')]/h2"
    li_xpath = "//following-sibling::ul[1]/li"

    regex_exclude = r'ausgenommen'

    NO_MATCH = -1
    NO_RISK = 0
    VARIANT = 1
    HI_INC = 2
    RISK = 3
    PARTIAL = 4

    risk_levels = ({'code': NO_RISK, 're': "^(?=.*risikogebiet)(?=.*kein)(?=.*(staat|region|gebiet)).*$"},
                   {'code': RISK, 're': "^(?=.*risikogebiet)(?=.*(staat|region|gebiet)).*$"},
                   {'code': HI_INC, 're': "^(?=.*hochinzidenz)(?=.*(staat|region|gebiet)).*$"},
                   {'code': VARIANT, 're': "^(?=.*virusvariant)(?=.*(staat|region|gebiet)).*$"},)
    risk_priority = (RISK, HI_INC, VARIANT, NO_RISK)    # Used to resolve duplicates

    separators = ("(", "inkl", "–", "-")
    deletable = ("(", ")", ":", "–")

    @classmethod
    def strip_country(cls, message):
        split_msg = message.split(" ")
        sep_index = len(split_msg)
        found = False
        for i, s in enumerate(split_msg):
            for sep in cls.separators:
                index = s.find(sep)
                if index == 0:
                    sep_index = i
                    found = True
                    break
                elif index != -1:
                    sep_index = i + 1
                    found = True
                    break
            if found:
                break

        name = " ".join(split_msg[:sep_index])
        info = " ".join(split_msg[sep_index:])

        return cls.clean(name), cls.unwrap(info)

    @classmethod
    def clean(cls, message):
        for d in cls.deletable:
            message = message.replace(d, '')
        return message

    @staticmethod
    def unwrap(message):
        match = re.search(r"^\([^()]+\)$", message)
        if match:
            return message[1:-1]
        else:
            return message

    @classmethod
    def extract_date(cls, info, preposition="seit"):
        re_fmt = cls.date_fmt["risk"]["re"]
        dt_fmt = cls.date_fmt["risk"]["dt"]
        fb_fmt = cls.date_fmt["risk"]["fallback"]
        ppt_fmt = rf'{preposition}[] +{re_fmt}'

        date_match = re.search(ppt_fmt, info, re.I)
        if date_match:
            prep_date = date_match.group()
            date = re.search(cls.date_fmt["risk"]["re"], prep_date, re.I).group()
            try:
                date_dt = dt.strptime(date, dt_fmt).date()
            except ValueError:
                date_dt = dt.strptime(date.replace('ä', ''), fb_fmt).date()     # Maerz workaround
            return date_dt
        else:
            return None

    def start_requests(self):
        urls = [
            'https://www.rki.de/risikogebiete',
        ]
        for url in urls:
            yield scrapy.Request(url=url, callback=self.parse)

    def parse(self, response):
        if response.status in (404, 500):
            raise RuntimeError(f"Site {response.url} not found")

        print(f"Scraping the following URL:\n{response.url}\n")

        stand = response.xpath("//div[contains(@class, 'subheadline')]/p/text()")
        match = re.search(self.date_fmt['de']['re'], stand.get())
        if not match:
            raise RuntimeError("Unable to find a date")

        db_date_df = pd.read_csv(date_path)
        db_date = dt.strptime(db_date_df.at[0, "report_date"], self.date_fmt['db']).date()

        res_date = dt.strptime(match.group(), self.date_fmt['de']['dt']).date()

        print(f"Saved data is from {db_date}, response data from {res_date} was found.\n")

        country_lut = self.country_names(german=True, lookup=True)

        name_err = []
        info_err = []
        dates_err = []
        risk_err = []
        exc_err = []

        db_old = pd.read_csv(db_path)
        db_old = db_old[db_old["ISO3_CODE"] != "ERROR"]

        db_regions = db_old[db_old['region'].notna()]
        db_regions = db_regions.assign(NAME_DE=db_regions["NAME_ENGL"].apply(de.gettext))
        reg_df = db_regions[["ISO3_CODE", "NAME_ENGL", "NAME_DE", "NUTS_CODE"]]

        db_old = db_old[~db_old['region'].notna()]
        iso3_names = db_old[["ISO3_CODE", "NAME_ENGL", "NAME_DE"]]
        iso3_en = iso3_names.set_index("ISO3_CODE")["NAME_ENGL"].to_dict()
        iso3_de_lut = iso3_names.set_index("NAME_DE")["ISO3_CODE"].to_dict()

        risk_headers = response.xpath(f"{self.h2_xpath}/text()")
        df_collector = {self.NO_RISK: None, self.RISK: None, self.HI_INC: None, self.VARIANT: None}

        name_regions = []
        info_regions = []
        iso3_regions = []
        nuts_regions = []
        risk_regions = []
        dates_regions = []

        for i_h, h in enumerate(risk_headers, 1):
            h_text = h.get()
            code = self.NO_MATCH
            for rl in self.risk_levels:
                if re.search(rl['re'], h_text, re.I):
                    code = rl['code']
                    break
            print(f"The following header has been assigned to risk_level_code {code}:")
            print(f"\t{h_text}\n")

            if code != self.NO_MATCH:
                date_ppt = "bis" if code == self.NO_RISK else "seit"

                states = response.xpath(f"({self.h2_xpath})[{i_h}]{self.li_xpath}")

                risk_dates = []

                name_states = []
                info_states = []
                iso3_states = []
                risk_states = []
                exc_states = []

                for i_s, s in enumerate(states, 1):
                    iso3_found = None
                    regions = response.xpath(f"({self.h2_xpath})[{i_h}]{self.li_xpath}[{i_s}]/ul/li/text()")
                    msg = s.get()[4:-5]     # Remove <li></li>
                    msg = msg.replace("<p>", "").replace("</p>", "")

                    reg_excluded = None
                    country_code = code
                    if code == self.RISK and len(regions) > 0:
                        country_code = self.PARTIAL
                        reg_excluded = bool(re.search(self.regex_exclude, msg, re.I))

                    name_scraped, info_scraped = self.strip_country(msg)
                    if name_scraped in iso3_de_lut.keys():      # Direct search from old DB
                        iso3_found = iso3_de_lut[name_scraped]
                    else:
                        for name, iso3 in country_lut.items():  # Exhaustive search with pycountry and alias
                            if name in name_scraped:
                                iso3_found = iso3
                                name_scraped = name
                                break
                    if not iso3_found:
                        for name, iso3 in country_lut.items():  # Repeat exhaustive search in the whole message
                            if name in msg:
                                name_scraped = name
                                info_scraped = self.clean(msg.replace(name, ""))
                                iso3_found = iso3
                                break
                    if not iso3_found:                          # Last check among the regions
                        region = db_regions[db_regions["NAME_DE"] == name_scraped]
                        if not region.empty:
                            name_regions.append(name_scraped)
                            risk_regions.append(country_code)
                            info_regions.append(info_scraped)
                            dates_regions.append(self.extract_date(info_scraped, preposition=date_ppt))
                            iso3_regions.append(region["ISO3_CODE"].iat[0])
                            nuts_regions.append(region["NUTS_CODE"].iat[0])
                            continue
                    if iso3_found:
                        name_states.append(name_scraped)
                        info_states.append(info_scraped)
                        risk_dates.append(self.extract_date(info_scraped, preposition=date_ppt))
                        iso3_states.append(iso3_found)
                        risk_states.append(country_code)
                        exc_states.append(reg_excluded)
                    else:
                        print(f"Unidentified state: {name_scraped}")
                        print(f"Risk level code:\n\t{country_code}")
                        print(f"Info:\n\t{msg}\n")

                        name_err.append(name_scraped)
                        info_err.append(msg)
                        dates_err.append(self.extract_date(msg, preposition=date_ppt))
                        risk_err.append(country_code)
                        exc_err.append(reg_excluded)
                    country_regs = reg_df[reg_df["ISO3_CODE"] == iso3_found]
                    reg_code = self.NO_RISK if reg_excluded else code
                    for r in regions:
                        name_sr, info_sr = self.strip_country(r.get())
                        nuts = country_regs[country_regs["NAME_DE"] == name_sr]["NUTS_CODE"]
                        nuts = None if nuts.empty else nuts.iloc[0]

                        name_regions.append(name_sr)
                        risk_regions.append(reg_code)
                        info_regions.append(info_sr)
                        dates_regions.append(self.extract_date(info_sr, preposition=date_ppt))
                        iso3_regions.append(iso3_found)
                        nuts_regions.append(nuts)

                df = pd.DataFrame({"ISO3_CODE": iso3_states, "risk_level_code": risk_states, "NAME_DE": name_states,
                                   "NAME_ENGL": [iso3_en[i3r] for i3r in iso3_states],
                                   "risk_date": risk_dates, "region": None, "REG_EXCLUDED": exc_states,
                                   "NUTS_CODE": None, "INFO_DE": info_states})
                df_collector[code] = df

        db_new = pd.concat([df_collector[i] for i in self.risk_priority])
        db_curated = db_new.drop_duplicates(subset="ISO3_CODE")

        df_regions = pd.DataFrame({"ISO3_CODE": iso3_regions, "risk_level_code": risk_regions,
                                   "NAME_DE": name_regions, "NAME_ENGL": name_regions,
                                   "region": True, "NUTS_CODE": nuts_regions,
                                   "risk_date": dates_regions, "INFO_DE": info_regions})
        df_regions = df_regions.sort_values("ISO3_CODE")

        df_unknown = pd.DataFrame({"NAME_DE": name_err, "risk_level_code": risk_err, "INFO_DE": info_err,
                                   "risk_date": dates_err, "REG_EXCLUDED": exc_err})
        df_unknown = df_unknown.assign(ISO3_CODE="ERROR", ERROR="UNKNOWN_AREA")

        df_duplicated = pd.concat([db_curated, db_new]).drop_duplicates(keep=False)
        df_duplicated = df_duplicated.assign(ISO3_CODE="ERROR", ERROR="DUPLICATED")

        print(f"Process summary:")
        print(f"\t- {len(db_curated)} states have been succesfully processed")
        print(f"\t- {len(df_regions)} regions have been identified")
        print(f"\t- {len(df_duplicated)} states are duplicated")
        print(f"\t- {len(df_unknown)} states could not be identified")

        db_norisk = db_old.assign(risk_level_code=lambda x: x.where(x["ISO3_CODE"] == "DEU",
                                                                    self.NO_RISK)["risk_level_code"])
        db_curated = pd.concat([db_curated,
                                db_norisk[["ISO3_CODE", "NAME_ENGL", "NAME_DE",
                                           "risk_level_code"]]]).drop_duplicates(subset="ISO3_CODE")
        db_curated = db_curated.sort_values("ISO3_CODE")

        db_final = pd.concat([db_curated, df_regions, df_duplicated, df_unknown]).set_index("ISO3_CODE")
        db_final.astype({"risk_level_code": int}).to_csv(db_path, encoding='utf-8-sig',
                                                         date_format=self.date_fmt['db'])

        pd.DataFrame({"report_date": [res_date]}).to_csv(date_path, index=False, date_format=self.date_fmt['db'])

    @classmethod
    def country_names(cls, german=True, lookup=True):

        countries = {k: list(v) for k, v in cls.alias.items()}

        for c in pycountry.countries:
            iso3 = c.alpha_3
            cname = c.name
            comma = False
            if ',' in cname:
                try:
                    cname = c.common_name
                except AttributeError:
                    comma = True
            try:
                cname_official = c.official_name
                c_names = [cname, cname_official]
                if comma:
                    c_names = c_names[::-1]
            except AttributeError:
                c_names = [cname]

            if german:
                c_names = [de.gettext(cn) for cn in c_names]

            try:
                countries[iso3] += c_names
            except KeyError:
                countries[iso3] = c_names

        if lookup:
            c_lut = {}
            while len(countries) > 0:
                deleted_iso3 = []
                for iso3 in countries.keys():
                    c_lut[countries[iso3].pop(0)] = iso3
                    if len(countries[iso3]) == 0:
                        deleted_iso3.append(iso3)
                for iso3 in deleted_iso3:
                    del countries[iso3]
            return c_lut
        else:
            return countries
