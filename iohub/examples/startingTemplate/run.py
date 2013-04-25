"""
ioHub
.. file: ioHub/examples/startingTemplate/run.py
"""

import iohub
from iohub.client import Computer
from iohub.util.experiment import ioHubExperimentRuntime

class ExperimentRuntime(ioHubExperimentRuntime):
    """
    Create an experiment using psychopy and the ioHub framework by extending 
    the ioHubExperimentRuntime class
    """
    def run(self,*args,**kwargs):
        """
        The run method contains your experiment logic. It is equal to what
        would be in your main psychopy experiment script.py file in a standard
        psychopy experiment setup. That is all there is too it really.
        """
        print "Hello World. Press any key to quit."
        self.hub.clearEvents('all')
        kb=self.devices.kb
        while not kb.getEvents():
            self.hub.wait(0.010)
        ### End of experiment logic

# The below code should never need to be changed, unless you want to get command
# line arguements or something. Otherwise, just copy it as is to a new experiment
# python file.
if __name__ == "__main__":
    def main(configurationDirectory):
        """
        Creates an instance of the ExperimentRuntime class, checks for an 
        experiment config file name parameter passed in via
        command line, and launches the experiment logic.
        """
        import sys
        if len(sys.argv)>1:
            configFile=sys.argv[1]
            runtime=ExperimentRuntime(configurationDirectory, configFile)
        else:
            runtime=ExperimentRuntime(configurationDirectory, "experiment_config.yaml")
    
        runtime.start()

    # The ioHub.module_directory function determines what the current directory is of
    # the function that is passed to it. It is more reliable when running scripts
    # via IDEs etc in terms of reporting the true file location. That is the claim
    # of the original function author at least. ;) It works, which is what matters.
    configurationDirectory=iohub.module_directory(main)

    # run the main function, which starts the experiment runtime
    main(configurationDirectory)

