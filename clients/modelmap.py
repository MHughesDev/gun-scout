"""Model-name -> (gun_type, action) inference.

Most listings name a model whose action every gun person knows (Remington 700
= bolt, 870 = pump, Glock = semi auto) but whose title never says so —
sites like GunsAmerica/gun.deals/Cabela's fill action for only 2-32% of
listings. This table encodes that domain knowledge so enrichment can fill
`action`/`gun_type` for the rest.

Rules are (brand, model_pattern, gun_type, action):
- brand: lowercase substring(s, '|'-separated) that must appear in the
  manufacturer OR title ('' = the model pattern is distinctive enough on its
  own, e.g. 'x-bolt'); numeric models (700, 870...) always carry a brand so
  'Savage 110' can't fire on a stray '110' elsewhere.
- model_pattern: regex searched (case-insensitively) in 'model + title'.
- FIRST matching rule wins, so specific rules go before generic fallbacks
  ('Henry Single Shot' before the Henry-defaults-to-lever rule), and rules
  for one brand's use of a name go before another's (Beretta 686 over/under
  outranks the S&W 686 revolver only because the S&W rule demands the
  Smith brand).

Vocabulary matches titleparse: gun_type in {pistol, revolver, rifle, shotgun,
muzzleloader, other}; action in {semi auto, bolt, lever, pump, revolver,
single shot, over/under, side by side, muzzleloader}.
"""
import re

# (brand, model_pattern, gun_type, action)
_RULES: list[tuple[str, str, str, str]] = [
    # ---- platform patterns (brand-independent, globally unambiguous) ------
    ("", r"\bar-?15\b|\bar-?10\b|\bar-?9\b|\blr-?308\b|\bsr-?25\b|\bm4e1\b|"
         r"\bm&p\s?15\b|\bxm-?15\b|\bpa-?15\b|\bam-?15\b|\bmk18\b|\bm16a?\d?\b|"
         r"\b6920\b|\b6720\b|\bcr67\d{2}\b|\bmodern sporting\b", "", "semi auto"),
    ("", r"\bak-?47\b|\bak-?74\b|\bakm\b|\bak-?v\b|\bzpap\b|\bwasr\b|\bvska\b|"
         r"\bras-?47\b|\bdraco\b|\bsks\b|\bgalil\b", "", "semi auto"),
    ("", r"\b(?:19|20)11\b|\b2k11\b", "pistol", "semi auto"),
    ("", r"\b10/22\b", "rifle", "semi auto"),
    ("", r"\bm1\s?garand\b|\bm1\s?carbine\b|\bm1a\b", "rifle", "semi auto"),
    ("", r"\bmp5\w*|\bsp5[lk]?\b|\bap5\b|\buzi\b|\bscorpion\b|\bstribog\b|"
         r"\bbanshee\b|\bsub-?2000\b|\bcx-?4\b|\bmpx\b|\bmcx\b|\bevo\s?3\b|"
         r"\bca9mm\b|\bar-style\b|\bfx9\b", "", "semi auto"),
    # lever guns named by their patent year ('Uberti 1873 Carbine') — must
    # outrank the 1873-revolver rule below
    ("", r"\b18(?:60|66|73|76|86|92|94)\b(?=.*(?:rifle|carbine|lever))|\byellowboy\b",
     "rifle", "lever"),
    ("", r"single\s?action\s?army\b|\bsaa\b|peacemaker", "revolver", "revolver"),
    ("", r"\bmosin\b|\bnagant m?91\b|\bk98k?\b|\bkar98\b|\bm48\b(?=.*(?:mauser|yugo))|"
         r"\bsmle\b|\benfield no\.?\s?[14]\b|\b03-?a3\b|\bm1903\b|\b1903a3\b|\bkrag\b|"
         r"\barisaka\b|\bcarcano\b|\bm1917\b|\bpattern 1[47]\b|\bk31\b|"
         r"\btype 99\b|\btype 38\b|\bswedish mauser\b", "rifle", "bolt"),
    ("glock", r".", "pistol", "semi auto"),
    ("", r"\bderringer\b", "pistol", "single shot"),
    ("bond arms", r".", "pistol", "single shot"),

    # ---- semi-auto pistols ------------------------------------------------
    ("", r"\bhellcat\b|\bxd-?[sme]?\d?\b|\bechelon\b|\bhi-?power\b|\bhigh power\b|"
         r"\bp365x?l?\b|\bp320\b|\bp250\b|\bp210\b|\bp22[0-9]\b|\bp23[0-9]\b|\bp938\b|"
         r"\bvp9\b|\bvp40\b|\bp30l?s?k?\b|\busp\b|\bhk45\b|\bp2000\b|"
         r"\bpdp\b|\bppq\b|\bp99\b|\bccp\b|\bpps\b|\bppk/?s?\b|\bpk380\b|\bwmp\b",
     "pistol", "semi auto"),
    ("sig", r"\bm1[78]\b", "pistol", "semi auto"),
    ("sig", r"\bcross\b", "rifle", "bolt"),
    ("", r"\btp9\b|\bmete\b|\brival-?s?\b|\bmc9\b|\btti combat\b|"
         r"\bm&p\s?(?:9|40|45|380)\b|"
         r"\bm&p\s?shield\b|\bshield\s?(?:plus|ez|2\.0)\b|\bbodyguard\s?(?:380|2\.0)|"
         r"\bcsx\b|\bsd9\b|\bsdve\b|\bequalizer\b|\bsw9\b|\bsigma\b|\bmc[12]s?c?\b",
     "pistol", "semi auto"),
    ("", r"\blcp\b|\blc9s?\b|\bec9s?\b|\bmax-?9\b|\bsecurity-?(?:9|380)\b|"
         r"\bsr9c?\b|\bsr40\b|\bsr45\b|\bp8[59]\b|\bp9[0-79]\b|\bp345\b|"
         r"\bamerican pistol\b|\bruger-?57\b|\brxm\b", "pistol", "semi auto"),
    ("ruger", r"\bmark\s?i+v?\b|\b22/45\b", "pistol", "semi auto"),
    ("taurus", r"\bg2c\b|\bg2s\b|\bg3c?x?l?\b|\bgx2\b|\bgx4\b|\btx22\b|\bth[94]0c?\b|"
               r"\bpt1911\b|\bpt92\b|\bspectrum\b|\bpt111\b|\bpt709\b|\bpt738\b",
     "pistol", "semi auto"),
    ("", r"\bapx\b|\bpx4\b|\b92[fx]s?r?\b|\b92 elite\b|\bm9a?[134]?\b(?=.*beretta)|"
         r"\btomcat\b|\bbobcat\b|\b21a\b|\b3032\b|\b80x\b|\bcheetah\b", "pistol", "semi auto"),
    ("cz", r"\bp-?10\s?[cfsm]?\b|\b75\s?(?:b|bd|compact|sp-?01|shadow)\b|\bshadow\s?2\b|"
           r"\b97b?e?\b|\bp-?0[79]\b|\bts\s?2\b|\b2075\b|\brami\b", "pistol", "semi auto"),
    ("", r"\bfive-?seven\b|\b509\b|\b510\b(?=.*fn)|\b545\b(?=.*fn)|\bfnx-?\d*\b|"
         r"\bfns-?\d*\b|\bfn 502\b|\breflex\b(?=.*fn)", "pistol", "semi auto"),
    ("", r"\bbuck ?mark\b|\b1911-?22\b|\b1911-?380\b", "pistol", "semi auto"),
    ("kimber", r"\bmicro\s?9?\b|\bultra carry\b|\bpro carry\b|\bcustom i+\b|"
               r"\braptor i+\b|\br7 mako\b|\bstainless i+\b|\bkds9c\b|\bsolo\b|"
               r"\brapide\b|\baegis\b|\bevo sp\b", "pistol", "semi auto"),
    ("kel", r"\bp-?17\b|\bp-?32\b|\bp-?3at\b|\bpf-?9\b|\bpmr-?30\b|\bcp33\b|\bp50\b",
     "pistol", "semi auto"),
    ("", r"\bcpx-?[123]\b|\bdvg-?1\b", "pistol", "semi auto"),   # SCCY
    ("hi-point|hi point|hipoint", r"\bc-?9\b|\bcf-?380\b|\bjcp\b|\bjhp\b|\bjxp\b|\byc9\b",
     "pistol", "semi auto"),
    ("", r"\bthunder\s?380\b|\bbp9cc\b|\btpr9\b|\bfirestorm\b", "pistol", "semi auto"),
    ("eaa|tanfoglio|girsan", r"\bwitness\b|\bregard\b|\bmc\s?28\b|\bmc\s?14\b",
     "pistol", "semi auto"),
    ("", r"\bstr-?9s?c?\b", "pistol", "semi auto"),              # Stoeger
    ("savage", r"\bstance\b", "pistol", "semi auto"),
    ("", r"\bstaccato\s?(?:c2?|cs|p|xc|xl)?\b", "pistol", "semi auto"),
    ("", r"\bdesert eagle\b|\bbaby eagle\b", "pistol", "semi auto"),
    ("", r"\bmr9\d{2}\b|\bcr9\d{2}\b|\bdr9\d{2}\b|\bxr9\d{2}\b|\boz-?9c?\b",
     "pistol", "semi auto"),
    ("", r"\bpx-?9\b|\bzigana\b|\bregent br9\b|\b9c1\b|\bdy9\w*\b",
     "pistol", "semi auto"),
    ("", r"\barex\s?delta", "pistol", "semi auto"),
    ("sar", r"\bsar-?9x?c?\b|\bk-?12\b|\bcm9\b|\b2000st\b", "pistol", "semi auto"),
    ("", r"\bjericho\b|\bmasada\s?(?:slim)?\b|\buzi pro\b", "pistol", "semi auto"),
    ("walther", r"\bp38\b|\bp5\b|\bp1\b", "pistol", "semi auto"),
    ("", r"\bmakarov\b|\bcz-?82\b|\bcz-?52\b|\btt-?33\b|\btokarev\b(?=.*pistol)",
     "pistol", "semi auto"),
    ("", r"\bluger\b(?=.*p-?08)|\bp08\b|\bbroomhandle\b|\bc96\b", "pistol", "semi auto"),
    ("colt", r"\bwoodsman\b|\bhuntsman\b|\bchallenger\b|\bgold cup\b|\bdelta elite\b|"
             r"\bcombat commander\b|\bcombat elite\b|\bmustang\b|"
             r"(?<!45-70\s)\bgovernment\b", "pistol", "semi auto"),
    ("", r"\bhigh standard\b.*\b(?:duramatic|supermatic|sport king|victor)\b",
     "pistol", "semi auto"),
    ("dan wesson", r"\bdwx\b|\bvalor\b|\bspecialist\b|\bguardian\b|\bvigil\b|"
                   r"\beco\b|\bpointman\b|\btcp\b|\ba2\b", "pistol", "semi auto"),

    # ---- revolvers --------------------------------------------------------
    ("henry", r"\brevolver\b", "revolver", "revolver"),
    # S&W J/K/L/N/X-frame model numbers + named lines (41/52/39/59/5906 etc
    # are semi-autos, so the numbers are an explicit list, not a wildcard).
    ("smith|s&w", r"\bmodel\s?(?:10|12|13|14|15|17|18|19|25|27|28|29|3[0-8]|48|49|"
                  r"57|58|60|6[3-9]|86|317|329|340|342|351|360|386|43[02]|438|"
                  r"44[26]|460|500|547|58[16]|610|617|619|620|625|627|629|63[2-9]|"
                  r"64[0-9]|65[0-9]|66[0-9]|686|69)\b", "revolver", "revolver"),
    ("smith|s&w", r"\b(?:governor|[jklnx]-?frame|bodyguard\s?38|638|642|442|340pd|"
                  r"360pd|686|586|629|627|617|610|66-|19-)\b", "revolver", "revolver"),
    ("", r"\blcrx?\b|\bsp-?101\b|\bgp-?100\b|\bredhawk\b|\bsuper redhawk\b|"
         r"\bblackhawk\b|\bsuper blackhawk\b|\bsingle-?\s?(?:six|seven|nine|ten)\b|"
         r"\bwrangler\b|\bvaquero\b|\bbearcat\b|\bold army\b", "revolver", "revolver"),
    ("ruger", r"\bbisley\b", "revolver", "revolver"),
    ("", r"\bpython\b|\banaconda\b(?!\s*\.?50)", "revolver", "revolver"),
    ("colt", r"\bking cobra\b|\bcobra\b|\bviper\b|\btrooper\b|\bdetective special\b|"
             r"\bofficial police\b|\bpolice positive\b|\bnew frontier\b|"
             r"\bfrontier scout\b|\bdiamondback\b|\bnew service\b|\bagent\b",
     "revolver", "revolver"),
    ("taurus", r"\b(?:85|856|605|608|66|65|942|327|44c?)\b|\bjudge\b|\braging\b|"
               r"\btracker\b|\bdefender\s?(?:856|605)\b", "revolver", "revolver"),
    ("", r"\bjudge\b|\braging (?:bull|hunter|judge)\b", "revolver", "revolver"),
    ("", r"\brough ?rider\b|\bbarkeep\b", "revolver", "revolver"),
    ("charter", r".", "revolver", "revolver"),
    ("", r"\bbulldog\b|\bundercover\b|\bpitbull\b", "revolver", "revolver"),
    ("", r"\brhino\b|\bk6s\b|\bkorth\b|\bbfr\b", "revolver", "revolver"),
    ("", r"\bnaa\b|\bnorth american arms\b|\bmini-?revolver\b", "revolver", "revolver"),
    ("", r"\bcattleman\b|\bel patr[oó]n\b|\bevil roy\b|\b1873\b|\b1875\b|\b1858\b|"
         r"\bschofield\b|\btop break\b", "revolver", "revolver"),
    ("rossi", r"\brm6[46]\b|\brp63\b|\b971\b|\b972\b|\b877\b", "revolver", "revolver"),
    ("dan wesson", r"\b715\b|\bmodel 15\b", "revolver", "revolver"),
    ("webley", r".", "revolver", "revolver"),
    ("", r"\bpietta\b|\b1851 navy\b|\b1860 army\b|\b1847 walker\b|\bdragoon\b",
     "revolver", "revolver"),

    # ---- bolt rifles ------------------------------------------------------
    ("remington", r"\b(?:700|721|722|725|770|710|715|788|78[13]|660|600|673|"
                  r"model seven|40-?x|513|541|581|582|591|592)\b", "rifle", "bolt"),
    ("winchester", r"\b(?:70|54|670|770)\b|\bxpr\b", "rifle", "bolt"),
    ("ruger", r"\bamerican\b|\bhawkeye\b|\bm77\b|\b77/(?:17|22|357|44)\b|"
              r"\bprecision rifle\b|\brpr\b|\bscout\b", "rifle", "bolt"),
    ("savage", r"\b(?:110|111|112|114|116|25|axis|impulse|rascal)\b|"
               r"\b1[0126]\b(?!\s*(?:ga|gauge))|\b9[03]r?\b|\bmark i+\b|"
               r"\bb\.?(?:17|22|mag)\b", "rifle", "bolt"),
    ("", r"\bstevens 200\b", "rifle", "bolt"),
    ("", r"\bx-?bolt\b|\ba-?bolt\b|\bab3\b", "rifle", "bolt"),
    ("tikka", r"\bt[13]x?\b", "rifle", "bolt"),
    ("sako", r"\b(?:85|90|75|a7|l579|l61r?|l691|trg|finn\w*)\b", "rifle", "bolt"),
    ("bergara", r"\bb-?14\b|\bbmr\b|\bpremier\b|\bhmr\b|\bwilderness\b",
     "rifle", "bolt"),
    ("", r"\bridgeline\b|\bmesa\b|\btraverse\b|\bmodern carbon\b|"
         r"\bmpr\b(?=.*christensen)", "rifle", "bolt"),
    ("weatherby", r"\bmark\s?v\b|\bvanguard\b|\b307\b", "rifle", "bolt"),
    ("howa", r".", "rifle", "bolt"),
    ("mossberg", r"\bpatriot\b|\bmvp\b|\b(?:802|817)\b|\b4x4\b", "rifle", "bolt"),
    ("cz", r"\b(?:452|455|457|511|512|527|550|557|600)\b", "rifle", "bolt"),
    ("kimber", r"\b84m\b|\bhunter\b|\bmontana\b|\badirondack\b|\bmountain ascent\b",
     "rifle", "bolt"),
    ("franchi", r"\bmomentum\b", "rifle", "bolt"),
    ("", r"\bwaypoint\b|\bmodel\s?2020\b", "rifle", "bolt"),      # Springfield
    ("barrett", r"\bmrad\b|\bfieldcraft\b", "rifle", "bolt"),
    ("", r"\bhavak\b", "rifle", "bolt"),                          # Seekins
    ("", r"\bmauser\s?(?:98|m98|m18|m12)\b|\bm1[28]\b(?=.*mauser)", "rifle", "bolt"),
    ("fierce", r".", "rifle", "bolt"),
    ("", r"\bcascade\b", "rifle", "bolt"),                        # CVA Cascade
    ("nosler", r"\bm21\b|\bm48\b", "rifle", "bolt"),
    ("steyr", r"\bmannlicher\b|\bpro hunter\b|\bscout\b|\bssg\b", "rifle", "bolt"),
    ("browning", r"\bt-?bolt\b", "rifle", "bolt"),
    ("marlin", r"\bxt-?(?:17|22)r?\b|\bx7\b|\b980s?\b|\b915\b|\b917\b|\b25n\b",
     "rifle", "bolt"),
    ("henry", r"\bmini bolt\b", "rifle", "bolt"),
    ("blaser", r"\br8\b|\br93\b", "rifle", "bolt"),
    ("", r"\baccumark\b|\bbackcountry\b(?=.*weatherby)", "rifle", "bolt"),
    ("", r"\bkodiak\b(?=.*(?:bolt|hunter))|\bcarbon king\b", "rifle", "bolt"),

    # ---- lever rifles -----------------------------------------------------
    ("henry", r"\bsingle\s?shot\b", "", "single shot"),   # before Henry default
    ("henry", r"\b(?:ar-?7|survival)\b|\bhomesteader\b", "rifle", "semi auto"),
    ("henry", r"\bpump\b", "rifle", "pump"),
    ("henry", r"\b\.?410\b|\bshotgun\b|\baxe\b", "shotgun", "lever"),
    ("henry", r".", "rifle", "lever"),                    # Henry default: lever
    ("winchester", r"\b(?:94|1894|92|1892|1886|1873|1866|71|88|9422|150|250)\b",
     "rifle", "lever"),
    ("marlin", r"\b(?:336\w*|1894\w*|1895\w*|1897|39a?\b|444\w*|93\b|95\b|"
               r"308mx|338mx|golden 39)", "rifle", "lever"),
    ("", r"\bblr\b", "rifle", "lever"),
    ("rossi", r"\br92\b|\br95\b|\bm92\b", "rifle", "lever"),
    ("savage", r"\b99c?\b", "rifle", "lever"),
    ("", r"\bbig boy\b|\bgolden boy\b|\blong ranger\b|\bside gate\b",
     "rifle", "lever"),

    # ---- semi-auto rifles -------------------------------------------------
    ("ruger", r"\bmini-?\s?(?:14|thirty|30)\b|\bpc carbine\b|\blc carbine\b|"
              r"\bsr-?556\b|\bar-?556\b|\b10-?22\b", "rifle", "semi auto"),
    ("remington", r"\b(?:552|550|740|742|7400|750|woodsmaster|speedmaster|"
                  r"nylon 66|model (?:8|81|24|241))\b", "rifle", "semi auto"),
    ("marlin", r"\b(?:60|795|70|7000|989|camp|papoose)\b", "rifle", "semi auto"),
    ("savage", r"\ba(?:17|22)\b|\b64f?l?\b", "rifle", "semi auto"),
    ("browning", r"\bbar\b|\bsa-?22\b|\b22 auto\b", "rifle", "semi auto"),
    ("winchester", r"\b(?:63|74|77|100|190|290|490)\b|\bwildcat\b", "rifle", "semi auto"),
    ("", r"\bsaint\b|\bsocom\s?16\b", "rifle", "semi auto"),
    ("", r"\btavor\b|\bcarmel\b|\bzion-?15\b|\bx95\b", "rifle", "semi auto"),
    ("", r"\bsu-?16\b|\brfb\b|\brdb\b|\bcmr-?30\b", "rifle", "semi auto"),
    ("", r"\b(?:995|1095|3895|4595)\s?ts\b", "rifle", "semi auto"),  # Hi-Point
    ("", r"\bbren\s?2\b|\bvz\.?\s?58\b", "rifle", "semi auto"),
    ("", r"\bscar\s?1[67]s?\b|\bps90\b|\bfn15\b|\bfnar\b|\bm249s\b", "rifle", "semi auto"),
    ("", r"\bmr556\b|\bhk416\b|\bhk91\b|\bhk93\b|\bptr-?91\b|"
         r"\bg3\b(?=.*(?:hk|h&k|ptr|clone|roller))", "rifle", "semi auto"),
    ("", r"\bvolunteer\b|\bfpc\b|\bresponse\b(?=.*(?:smith|s&w))", "rifle", "semi auto"),
    ("mossberg", r"\bblaze\s?47?\b|\b715t\b|\b702\b|\bplinkster\b", "rifle", "semi auto"),
    ("", r"\b1927a-?1\b|\bthompson\b(?=.*(?:auto-?ordnance|1927|tommy))",
     "rifle", "semi auto"),
    ("", r"\bmini draco\b|\bmicro draco\b", "pistol", "semi auto"),
    ("", r"\bsam7\b|\bslr-?10[67]\b|\bkr-?103\b|\bkp-?9\b|\bkr-?9\b", "", "semi auto"),
    ("", r"\bgar(?:and)?\b(?=.*m1)", "rifle", "semi auto"),

    # ---- pump rifles ------------------------------------------------------
    ("remington", r"\b(?:7600|760|572|121|141)\b", "rifle", "pump"),
    ("winchester", r"\b(?:61|62a?|1890|1906)\b", "rifle", "pump"),

    # ---- single-shot rifles -----------------------------------------------
    ("ruger", r"\b(?:no\.?\s?1|number 1)\b", "rifle", "single shot"),
    ("", r"\bencore\b|\bcontender\b", "", "single shot"),
    ("", r"\bhandi-?rifle\b|\bpardner\b|\btopper\b", "", "single shot"),
    ("", r"\b1885\b|\bhigh wall\b|\blow wall\b|\bsharps\b|\b1874\b|\btrapdoor\b|"
         r"\brolling block\b|\bfalling block\b|\bmartini\b", "rifle", "single shot"),
    ("cva", r"\bscout\b", "rifle", "single shot"),
    ("", r"\bcrackshot\b|\boutfitter g[23]\b", "rifle", "single shot"),

    # ---- pump shotguns ----------------------------------------------------
    ("remington", r"\b(?:870|887|31)\b", "shotgun", "pump"),
    ("mossberg|maverick", r"\b(?:500|505|510|535|590[asm]?\d?|835|88)\b|\bshockwave\b",
     "shotgun", "pump"),
    ("winchester", r"\bsxp\b|\b1300\b|\b1200\b|\bmodel 12\b|\bmodel 97\b|\b1897\b",
     "shotgun", "pump"),
    ("", r"\bnova\b|\bsupernova\b", "shotgun", "pump"),
    ("", r"\bbps\b", "shotgun", "pump"),
    ("ithaca", r"\b37\b|\b87\b|\bmodel 37\b", "shotgun", "pump"),
    ("", r"\bksg\b|\bks7\b|\bdp-?12\b", "shotgun", "pump"),
    ("stevens", r"\b320\b|\b77\b", "shotgun", "pump"),
    ("", r"\bp-?3000\b|\bp-?3500\b", "shotgun", "pump"),          # Stoeger
    ("cz", r"\b6(?:12|20)\b", "shotgun", "pump"),
    ("weatherby", r"\bpa-?459\b|\bpa-?08\b", "shotgun", "pump"),
    ("tristar", r"\bcobra\b", "shotgun", "pump"),

    # ---- semi-auto shotguns -----------------------------------------------
    ("remington", r"\b(?:1100|11-?87|v3|versa\s?max|model 11|11-?48|58|878|"
                  r"sportsman)\b", "shotgun", "semi auto"),
    ("mossberg", r"\b(?:930|935|940|9200|sa-?(?:20|28|410))\b", "shotgun", "semi auto"),
    ("benelli", r"\bm[124]\b|\bmontefeltro\b|\bsbe\b|\bsuper black eagle\b|"
                r"\bethos\b|\blegacy\b|\bcordoba\b|\bvinci\b|\bultra\s?light\b",
     "shotgun", "semi auto"),
    ("beretta", r"\b(?:a300|a350|a400|a390|390|303|al391|391|1301|a303|xtrema)\b",
     "shotgun", "semi auto"),
    ("browning", r"\b(?:a5|auto-?5|maxus|gold|silver)\b", "shotgun", "semi auto"),
    ("winchester", r"\bsx-?[234e]\b|\bsuper x\b|\bmodel 50\b|\bmodel 59\b",
     "shotgun", "semi auto"),
    ("franchi", r"\baffinity\b|\bintensity\b|\b48\s?al\b", "shotgun", "semi auto"),
    ("", r"\bm3[05]00\b|\bm3020\b", "shotgun", "semi auto"),      # Stoeger
    ("cz", r"\b(?:712|720|912|1012)\b", "shotgun", "semi auto"),
    ("weatherby", r"\b18i\b|\bsa-?08\b|\belement\b", "shotgun", "semi auto"),
    ("tristar", r"\bviper\b|\braptor\b", "shotgun", "semi auto"),
    ("", r"\bmc312\b|\bmc212\b|\brenegauge\b|\bvr(?:80|82|99)\b|\bsaiga\b",
     "shotgun", "semi auto"),

    # ---- over/under & side-by-side ----------------------------------------
    ("", r"\bcitori\b|\bcynergy\b|\bsuperposed\b|\b725\b(?=.*browning)",
     "shotgun", "over/under"),
    ("beretta", r"\b(?:686|687|682|690|69[24]|silver pigeon|dt-?1[01]|"
                r"ultraleggero|onyx|white wing)\b", "shotgun", "over/under"),
    ("cz", r"\bdrake\b|\bredhead\b|\bupland\b|\ball-?american\b|\bsupreme field\b|"
           r"\breaper magnum\b|\bwingshooter\b", "shotgun", "over/under"),
    ("franchi", r"\binstinct\b", "shotgun", "over/under"),
    ("", r"\bcondor\b|\bsilver reserve\b|\bgold reserve\b|\borion\b",
     "shotgun", "over/under"),
    ("tristar", r"\bsetter\b|\btrinity\b|\bhunter mag\b", "shotgun", "over/under"),
    ("", r"\bred label\b|\bstevens 555\b|\b555\b(?=.*stevens)", "shotgun", "over/under"),
    ("winchester", r"\bmodel 101\b|\b101 field\b", "shotgun", "over/under"),
    ("", r"\bk-?80\b|\bkrieghoff\b|\bperazzi\b|\bcaesar guerini\b|"
         r"\bf3\b(?=.*blaser)|\bfausti\b|\brizzini\b", "shotgun", "over/under"),
    ("", r"\buplander\b|\bcoach gun\b|\bbobwhite\b|\bsharp-?tail\b|\bhammer coach\b|"
         r"\bstevens 311\b|\blc smith\b|\bfox model b\b|\bside-?by-?side\b",
     "shotgun", "side by side"),

    # ---- single-shot / bolt / lever shotguns ------------------------------
    ("", r"\bstevens 301\b|\b301 turkey\b|\btuffy\b", "shotgun", "single shot"),
    ("savage", r"\b2(?:12|20)\b(?=.*(?:ga|gauge|slug))", "shotgun", "bolt"),
    ("", r"\b1887\b|\b9410\b", "shotgun", "lever"),

    # ---- muzzleloaders ----------------------------------------------------
    ("", r"\bmuzzleloader\b|\bflintlock\b|\bpercussion\b|\bcaplock\b|"
         r"\bblack\s?powder\b|\baccura\b|\boptima\b|\bparamount\b|\bnitrofire\b|"
         r"\bbuckstalker\b|\bwolf\b(?=.*cva)|\bpursuit\b(?=.*traditions)",
     "muzzleloader", "muzzleloader"),
]

_COMPILED: list[tuple[list[str], re.Pattern, str, str]] | None = None


def _rules() -> list[tuple[list[str], re.Pattern, str, str]]:
    global _COMPILED
    if _COMPILED is None:
        _COMPILED = [([b for b in brand.split("|") if b],
                      re.compile(pat, re.I), gt, act)
                     for brand, pat, gt, act in _RULES]
    return _COMPILED


def infer(manufacturer: str, model: str, title: str) -> tuple[str, str]:
    """Best-guess (gun_type, action) from the model/title. Either may be ''."""
    hay_brand = f"{manufacturer or ''} {title or ''}".lower()
    hay_model = f"{model or ''} {title or ''}"
    for brands, rex, gun_type, action in _rules():
        if brands and not any(b in hay_brand for b in brands):
            continue
        if rex.search(hay_model):
            return gun_type, action
    return "", ""
