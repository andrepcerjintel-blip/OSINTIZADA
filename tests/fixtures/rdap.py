"""Respostas RDAP no formato RFC 9083 (estrutura baseada em respostas reais da ARIN/Verisign)."""

IP_8_8_8_8 = {
    "objectClassName": "ip network",
    "handle": "NET-8-8-8-0-2",
    "startAddress": "8.8.8.0",
    "endAddress": "8.8.8.255",
    "ipVersion": "v4",
    "name": "GOGL",
    "type": "DIRECT ALLOCATION",
    "parentHandle": "NET-8-0-0-0-0",
    "cidr0_cidrs": [{"v4prefix": "8.8.8.0", "length": 24}],
    "arin_originas0_originautnums": [15169],
    "events": [{"eventAction": "registration", "eventDate": "2023-12-28T17:24:33-05:00"},
               {"eventAction": "last changed", "eventDate": "2023-12-28T17:24:56-05:00"}],
    "entities": [{
        "objectClassName": "entity", "handle": "GOGL", "roles": ["registrant"],
        "vcardArray": ["vcard", [["version", {}, "text", "4.0"], ["fn", {}, "text", "Google LLC"],
                                 ["adr", {"label": "1600 Amphitheatre Parkway\nMountain View\nCA\n94043\nUnited States"},
                                  "text", ["", "", "", "", "", "", ""]],
                                 ["kind", {}, "text", "org"]]],
        "entities": [{
            "objectClassName": "entity", "handle": "ABUSE5250-ARIN", "roles": ["abuse"],
            "vcardArray": ["vcard", [["version", {}, "text", "4.0"], ["fn", {}, "text", "Abuse"],
                                     ["kind", {}, "text", "group"],
                                     ["email", {}, "text", "network-abuse@google.com"]]],
        }],
    }],
}

AUTNUM_15169 = {
    "objectClassName": "autnum", "handle": "AS15169", "startAutnum": 15169, "endAutnum": 15169, "name": "GOOGLE",
    "events": [{"eventAction": "registration", "eventDate": "2000-03-30T00:00:00-05:00"}],
    "entities": [{"objectClassName": "entity", "handle": "GOGL", "roles": ["registrant"],
                  "vcardArray": ["vcard", [["fn", {}, "text", "Google LLC"], ["kind", {}, "text", "org"]]]}],
}

DOMAIN_EXAMPLE = {
    "objectClassName": "domain", "handle": "2336799_DOMAIN_COM-VRSN", "ldhName": "EXAMPLE.COM",
    "status": ["client delete prohibited", "client transfer prohibited"],
    "secureDNS": {"delegationSigned": True},
    "events": [{"eventAction": "registration", "eventDate": "1995-08-14T04:00:00Z"},
               {"eventAction": "expiration", "eventDate": "2026-08-13T04:00:00Z"}],
    "nameservers": [{"objectClassName": "nameserver", "ldhName": "A.IANA-SERVERS.NET"},
                    {"objectClassName": "nameserver", "ldhName": "B.IANA-SERVERS.NET"}],
    "entities": [
        {"objectClassName": "entity", "handle": "376", "roles": ["registrar"],
         "publicIds": [{"type": "IANA Registrar ID", "identifier": "376"}],
         "vcardArray": ["vcard", [["fn", {}, "text", "RESERVED-Internet Assigned Numbers Authority"]]]},
        {"objectClassName": "entity", "roles": ["registrant"],
         "vcardArray": ["vcard", [["fn", {}, "text", "REDACTED FOR PRIVACY"],
                                  ["email", {}, "text", "Please query the RDDS service of the Registrar"]]]},
    ],
}
