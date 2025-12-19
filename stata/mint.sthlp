{smcl}
{* *! version 1.0.0  19dec2024}{...}
{vieweralsosee "mint" "mint_installer"}{...}
{viewerjumpto "Syntax" "mint##syntax"}{...}
{viewerjumpto "Description" "mint##description"}{...}
{viewerjumpto "Options" "mint##options"}{...}
{viewerjumpto "Examples" "mint##examples"}{...}
{viewerjumpto "Remarks" "mint##remarks"}{...}
{title:Title}

{p2colset 5 18 20 2}{...}
{p2col :{hi:mint} {hline 2}}Create standardized research project repositories{p_end}
{p2colreset}{...}

{marker syntax}{...}
{title:Syntax}

{p 8 16 2}
{cmd:mint}
{cmd:,}
{cmdab:t:ype}({it:string})
{cmdab:n:ame}({it:string})
[{cmdab:p:ath}({it:string})
{cmdab:nog:it}
{cmdab:nod:vc}
{cmdab:b:ucket}({it:string})]

{marker description}{...}
{title:Description}

{pstd}
{cmd:mint} creates standardized project repositories using the mint Python package.
This command provides Stata users with easy access to create data repositories,
research projects, and infrastructure packages with proper versioning and structure.

{pstd}
The command supports three project types:
{p2colset 9 18 20 2}
{p2col :{it:data}}Data product repositories ({cmd:data_}{it:name}){p_end}
{p2col :{it:project}}Research project repositories ({cmd:prj__}{it:name}){p_end}
{p2col :{it:infra}}Infrastructure package repositories ({cmd:infra_}{it:name}){p_end}
{p2colreset}

{pstd}
Projects are created with:
{p2colset 9 18 20 2}
{p2col :Git}Version control initialization{p_end}
{p2col :DVC}Data version control setup{p_end}
{p2col :Templates}Standardized directory structures{p_end}
{p2col :Dependencies}Language-specific requirements files{p_end}
{p2colreset}

{marker options}{...}
{title:Options}

{p2colset 5 18 20 2}{...}
{p2col :{cmdab:t:ype}({it:string})}Project type: {cmd:data}, {cmd:project}, or {cmd:infra}{p_end}
{p2col :{cmdab:n:ame}({it:string})}Project name (required){p_end}
{p2col :{cmdab:p:ath}({it:string})}Output directory (default: current directory){p_end}
{p2col :{cmdab:nog:it}}Skip Git repository initialization{p_end}
{p2col :{cmdab:nod:vc}}Skip DVC initialization{p_end}
{p2col :{cmdab:b:ucket}({it:string})}Override default DVC bucket name{p_end}
{p2colreset}{...}

{marker examples}{...}
{title:Examples}

{pstd}Create a data repository:{p_end}
{phang2}{cmd:. mint, type(data) name(medicare_claims)}{p_end}

{pstd}Create a research project in a specific location:{p_end}
{phang2}{cmd:. mint, type(project) name(hospital_closures) path(/path/to/repos)}{p_end}

{pstd}Create an infrastructure package without Git initialization:{p_end}
{phang2}{cmd:. mint, type(infra) name(price_index) nogit}{p_end}

{pstd}Create a project with custom DVC bucket:{p_end}
{phang2}{cmd:. mint, type(data) name(mydata) bucket(my-custom-bucket)}{p_end}

{pstd}Use the resulting project path:{p_end}
{phang2}{cmd:. mint, type(project) name(analysis)}{p_end}
{phang2}{cmd:. display "`project_path'"}{p_end}

{marker remarks}{...}
{title:Remarks}

{pstd}
This command requires Stata 16+ with Python integration enabled.
The mint Python package must be installed and available in Stata's Python environment.

{pstd}
After successful project creation, the project path is stored in the local macro
{cmd:project_path} for programmatic use.

{pstd}
For more information about project structures and configuration, see the mint documentation.

{marker author}{...}
{title:Author}

{pstd}
mint development team{p_end}

{pstd}
For questions or issues, please refer to the mint documentation or repository.{p_end}